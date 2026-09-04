from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

from base.model import ModelBase
from modules.core.models import Merchant, TenantScoped, User
from modules.twin.choices import CatalogReadTool, IngestJobStatus, WebhookHitOutcome


class Event(TenantScoped):
    razorpay_event_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    signature_valid = models.BooleanField()
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "event_type", "received_at"])]

    def __str__(self):
        return f"{self.event_type} {self.razorpay_event_id or '(no id)'}"


class WebhookHit(ModelBase):
    """One hit to RazorpayWebhookAV, whatever happened to it.

    Not TenantScoped: several outcomes (unknown merchant, an unconfigured
    legacy route, an unresolved legacy tenant) never have a tenant to attach.
    Not unique on razorpay_event_id either, unlike Event -- this is a hit log,
    not an idempotency key, so the same event id legitimately appears more
    than once (every redelivery).

    tenant uses SET_NULL rather than Event's CASCADE: an audit trail should
    outlive the merchant row it describes.
    """

    tenant = models.ForeignKey(Merchant, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+")
    merchant_domain = models.CharField(max_length=255, blank=True, default="")

    outcome = models.SmallIntegerField(choices=WebhookHitOutcome.choices, db_index=True)

    razorpay_event_id = models.CharField(max_length=128, blank=True, default="")
    event_type = models.CharField(max_length=64, blank=True, default="")
    signature_valid = models.BooleanField(null=True, blank=True)
    error_detail = models.CharField(max_length=255, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "received_at"])]

    def __str__(self):
        return f"{self.get_outcome_display()} {self.razorpay_event_id or '(no id)'}"


class Product(TenantScoped):
    """Catalog row normalized from a product feed, else sitemap + JSON-LD.

    confidence records how much we trust the extraction: a Google/Meta feed is
    near 1.0, a JSON-LD scrape less. Anything low gets re-verified against the
    live PDP before it can be sold.
    """
    url = models.URLField(max_length=1024)
    sku = models.CharField(max_length=128, blank=True)
    title = models.CharField(max_length=512)
    price_paise = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    in_stock = models.BooleanField(null=True)
    image_url = models.URLField(max_length=1024, blank=True)
    is_visible_to_agents = models.BooleanField(default=True)
    raw = models.JSONField(default=dict)
    confidence = models.FloatField(default=0.0)
    source = models.CharField(max_length=32, default="jsonld")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "url"], name="uniq_tenant_product_url"),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_visible_to_agents"],
                         name="idx_product_tenant_visible"),
        ]

    def __str__(self):
        return self.title


class CatalogRead(TenantScoped):
    """How often agents read this store's catalogue. A counter, not an event log.

    Deliberately NOT a LedgerEntry. append() takes select_for_update on the
    Merchant row -- the same lock allocate_order_number() and every policy
    change take -- so counting a browse there would serialise agent searches
    behind checkout, and would put a write on the money path's lock for a number
    nobody audits. A row here is touched by a single-row UPDATE that locks
    nothing else in the store.

    Not an event log either: one row per search is unbounded, and the only
    questions ever asked of it are counts.
    """

    agent_label = models.CharField(max_length=64, blank=True)

    day = models.DateField()

    tool = models.CharField(max_length=32, choices=CatalogReadTool.choices)
    count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "agent_label", "day", "tool"],
                name="uniq_catalog_read_bucket",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "day"], name="idx_catalog_read_tenant_day"),
        ]

    def __str__(self):
        return f"{self.day} {self.agent_label or '(unknown)'} {self.tool} x{self.count}"

    @classmethod
    def record(cls, tenant, tool, agent_label="", *, day=None, delta=1) -> None:
        """Count one catalogue read.

        One UPDATE in the steady state. The first read of a
        (tenant, agent, day, tool) bucket costs an INSERT, and only a lost race
        on that first read costs a second UPDATE.

        UPDATE ... SET count = count + 1, not read-modify-write: two agents
        searching at once must not both read 4 and both write 5. The row lock
        the UPDATE takes is on this counter row alone -- no Merchant lock, which
        is the entire reason this table exists instead of a ledger append.
        tests/test_catalog_reads.py asserts that, rather than trusting it.

        Two shapes that look right and are not:
        bulk_create(update_conflicts=True) cannot express a counter -- Django's
        Postgres backend emits `SET col = EXCLUDED.col`, an overwrite, and
        supports no expression there. update_or_create goes through save(),
        which fires auto_now.

        unfiltered_objects, not objects: DeleteStatusManager hides
        status=DELETED, so a counter someone soft-deleted would make the UPDATE
        match nothing, the INSERT conflict with the surviving row, and the retry
        match nothing again -- a silent, permanent no-op. Counters are never
        soft-deleted; this makes that assumption unable to fail quietly.

        Exactness, stated plainly: increments of an existing bucket are exact,
        resolved in the database under the row lock. The one lossy window is a
        bucket's very first read, and the IntegrityError branch closes it.
        """
        tool = CatalogReadTool(tool)
        day = day or timezone.localdate()
        bucket = {"tenant": tenant, "agent_label": agent_label or "",
                  "day": day, "tool": tool}
        rows = cls.unfiltered_objects.filter(**bucket)

        if rows.update(count=F("count") + delta, last_read_at=timezone.now()):
            return
        try:
            with transaction.atomic():
                cls.objects.create(**bucket, count=delta, last_read_at=timezone.now())
        except IntegrityError:
            rows.update(count=F("count") + delta, last_read_at=timezone.now())


class IngestJob(TenantScoped):
    """One asynchronous catalog-ingestion run, started by a merchant user.

    `uuid` (from ModelBase) is the public identifier used in the /ingest and
    SSE URLs, never the integer pk. `created_by` is the authorization anchor
    for the SSE stream: only that user may open this job's event stream.
    """
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="ingest_jobs")

    source = models.URLField(max_length=1024)
    match = models.CharField(max_length=255, blank=True)
    limit = models.IntegerField(default=0)

    job_status = models.SmallIntegerField(
        choices=IngestJobStatus.choices, default=IngestJobStatus.PENDING,
    )

    total_urls = models.IntegerField(default=0)
    processed = models.IntegerField(default=0)
    created_count = models.IntegerField(default=0)
    updated_count = models.IntegerField(default=0)
    skipped_count = models.IntegerField(default=0)

    last_product_url = models.URLField(max_length=1024, blank=True)
    last_product_title = models.CharField(max_length=512, blank=True)

    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "job_status"])]

    def __str__(self):
        return f"IngestJob({self.uuid}) {self.get_job_status_display()}"


from modules.twin.orders import Order
