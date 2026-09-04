"""The order record.

Until now an order existed only as scattered evidence: two ledger entries, a
Razorpay Payment Link, and four webhook events. That is enough to prove what
happened and useless for answering "show me my orders", which both dashboards
are built around. This is the row that ties those pieces to a buyer, a
merchant, and the agent that placed it.

Deliberately NOT the source of truth for money -- the ledger is. This is a
projection built for reading.
"""
import uuid6
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from modules.core.models import Buyer, Merchant, TenantScoped
from modules.policy.models import Mandate

ORDER_NUMBER_PREFIX = "MNM"

ORDER_NUMBER_ATTEMPTS = 3


def allocate_order_number(tenant) -> str:
    """The next human order number for this store.

    MUST be called inside a transaction. The lock is Merchant's own row -- the
    same one LedgerEntry.append() takes -- so two concurrent checkouts on one
    store serialise here and on the chain in the same order, and two stores
    never block each other.

    The unique constraint on (tenant, order_number) is the backstop, not the
    mechanism: a per-tenant counter read outside a lock hands the same number
    to both callers, and the second insert would simply fail.
    """
    merchant = Merchant.objects.select_for_update().get(pk=tenant.pk)
    merchant.order_seq += 1
    merchant.save(update_fields=["order_seq"])
    return f"{ORDER_NUMBER_PREFIX}-{merchant.order_seq:04d}"


def create_order(**fields) -> "Order":
    """The only correct way to insert an order.

    Order.objects.create() on its own cannot satisfy the model: order_number is
    NOT NULL-ish by check constraint and unique per tenant, and the number has
    to come from the store's counter under that store's row lock. This wraps
    the two together, and it is what both checkout and the tests go through --
    a caller that hand-rolls the insert gets an IntegrityError, by design.

    The transaction is narrow on purpose. Callers create a Payment Link on
    Razorpay BEFORE reaching here, and that must stay outside: it is a network
    side effect no rollback can undo, and holding a row lock across it would
    serialise every checkout in the store behind one HTTP round trip. What does
    need to be atomic is the counter bump and the insert consuming it -- a bump
    that commits without its order leaves a gap in the numbering, and an insert
    that commits without its bump reuses a number.
    """
    tenant = fields["tenant"]
    for attempt in range(1, ORDER_NUMBER_ATTEMPTS + 1):
        try:
            with transaction.atomic():
                return Order.objects.create(
                    order_number=allocate_order_number(tenant), **fields)
        except IntegrityError:
            if attempt == ORDER_NUMBER_ATTEMPTS:
                raise


class Order(TenantScoped):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Payment failed"
        EXPIRED = "expired", "Link expired"

    uuid = models.UUIDField(default=uuid6.uuid6, unique=True, editable=False)

    order_number = models.CharField(max_length=32, blank=True, db_index=True)

    buyer = models.ForeignKey(Buyer, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="orders")
    mandate = models.ForeignKey(Mandate, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="orders")

    ledger_seq = models.BigIntegerField(null=True, blank=True)

    step_up = models.OneToOneField("policy.StepUp", on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="order")

    product_url = models.URLField(max_length=1024)
    sku = models.CharField(max_length=128, blank=True)
    title = models.CharField(max_length=512)
    image_url = models.URLField(max_length=1024, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price_paise = models.BigIntegerField()
    amount_paise = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="INR")

    llm_client = models.CharField(max_length=64, blank=True)
    llm_client_raw = models.CharField(max_length=255, blank=True)

    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    plink_id = models.CharField(max_length=64, blank=True, db_index=True)
    short_url = models.URLField(max_length=512, blank=True)
    reference_id = models.CharField(max_length=64, blank=True, db_index=True)

    paid_at = models.DateTimeField(null=True, blank=True)

    failed_attempts = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)

    failure_reason_code = models.CharField(max_length=64, blank=True)

    authorized_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    class Meta:
        ordering = ["-authorized_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "order_number"],
                                    name="uniq_order_number_per_tenant"),
            models.CheckConstraint(condition=~models.Q(order_number=""),
                                   name="order_number_not_blank"),
        ]
        indexes = [
            models.Index(fields=["tenant", "-authorized_at", "-id"],
                         name="idx_order_tenant_authz"),
            models.Index(fields=["buyer", "-authorized_at"], name="idx_order_buyer_authz"),
            models.Index(fields=["tenant", "status", "authorized_at"]),
            models.Index(fields=["tenant", "status", "paid_at"],
                         name="idx_order_tenant_paid_at"),
            models.Index(fields=["tenant", "ledger_seq"], name="idx_order_tenant_ledger_seq"),
        ]

    def __str__(self):
        return f"{self.title[:40]} x{self.quantity} ({self.status})"

    @property
    def rupees(self) -> str:
        return f"{self.amount_paise / 100:,.2f}"

    def mark_paid(self, when=None) -> None:
        self.status = self.Status.PAID
        self.paid_at = when or timezone.now()
        self.save(update_fields=["status", "paid_at"])
