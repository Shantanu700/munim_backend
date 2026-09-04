import uuid

from django.db import models, transaction
from django.utils import timezone

from base.enums import Status
from base.model import ModelBase
from modules.core.models import Buyer, Merchant, TenantScoped
from modules.policy.choices import OnBreach, RuleKind, RuleState


class Mandate(TenantScoped):
    """A buyer's signed authority for one agent to spend on one store.

    The JWS is the authority; this row is the state the signature cannot carry.
    A signature cannot be revoked and cannot count, so revocation and
    accumulated spend live here -- but the CAPS are read from the verified
    claims at decision time, never from this row. The columns below are a
    convenience copy for the admin and for issuing; editing them changes
    nothing about what gets allowed, which is exactly the property the
    signature is there to provide.
    """
    jti = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kid = models.CharField(max_length=32)
    subject = models.CharField(max_length=128, help_text="Which agent this authorises")
    buyer = models.ForeignKey(Buyer, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="mandates")

    per_txn_cap_paise = models.BigIntegerField()
    total_cap_paise = models.BigIntegerField()
    step_up_ceiling_paise = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    allowed_categories = models.JSONField(default=list, blank=True,
                                          help_text="Empty means any category")

    not_before = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    spent_paise = models.BigIntegerField(default=0)

    token = models.TextField(help_text="The issued JWS, kept for audit and re-display")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} @ {self.tenant.domain} ({self.jti})"

    def is_live(self, now=None) -> bool:
        now = now or timezone.now()
        return self.revoked_at is None and self.not_before <= now < self.expires_at

    @property
    def remaining_paise(self) -> int:
        return max(0, self.total_cap_paise - self.spent_paise)

    def record_spend(self, paise: int) -> None:
        """Called once a payment is confirmed, not when one is authorised."""
        with transaction.atomic():
            Mandate.objects.filter(pk=self.pk).update(
                spent_paise=models.F("spent_paise") + paise
            )
            self.refresh_from_db(fields=["spent_paise"])


class StorePolicy(ModelBase):
    """A named set of the merchant's own rules. Many per store, at most one live.

    A merchant keeps several of these -- a strict one, a sale one, whatever
    they need -- and activates one at a time. The rules live in PolicyRule
    rows; this is the row that says which set is in force.

    Optional by design, and that has teeth: with no active policy the engine
    applies NO store rules at all. What still applies is everything that is
    not a rule -- the kill switch on Merchant, the mandate's signed ceilings,
    catalogue membership, stock, and the exact price match. A store is
    agent-addressable the moment it is ingested and tightens deliberately.
    """
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE,
                                 related_name="store_policies")
    name = models.CharField(max_length=100, help_text="What the merchant calls this set")
    is_active = models.BooleanField(
        default=False,
        help_text="At most one per merchant. Never read on its own -- a soft-deleted "
                  "row can still hold True; see services.active_for.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    agent_traffic_enabled = models.BooleanField(default=True, editable=False)
    max_txn_paise = models.BigIntegerField(null=True, blank=True, editable=False)
    allowed_categories = models.JSONField(default=list, blank=True, editable=False)
    min_confidence = models.FloatField(default=0.0, editable=False)
    currency = models.CharField(max_length=3, default="INR", editable=False)

    class Meta:
        verbose_name_plural = "store policies"
        ordering = ["-is_active", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["merchant"],
                condition=models.Q(is_active=True) & ~models.Q(status=Status.DELETED),
                name="uniq_active_policy_per_merchant",
            ),
            models.UniqueConstraint(
                fields=["merchant", "name"],
                condition=~models.Q(status=Status.DELETED),
                name="uniq_policy_name_per_merchant",
            ),
            models.CheckConstraint(condition=~models.Q(name=""), name="policy_name_not_blank"),
        ]

    def __str__(self):
        return f"{self.name} @ {self.merchant.domain}"

    @property
    def is_live(self) -> bool:
        """The invariant, spelled out once. `is_active` alone is not it: the
        partial unique index ignores deleted rows, so a soft-deleted policy
        frees the active slot while its own column still reads True."""
        return self.is_active and self.status != Status.DELETED


class PolicyRule(ModelBase):
    """One configurable rule inside a policy.

    The kinds are fixed in registry.py, not invented by merchants -- each needs
    a checker and a place in the engine's sequence. A policy gets one row per
    kind when it is created; the merchant then configures each and decides
    whether it enforces.
    """
    policy = models.ForeignKey(StorePolicy, on_delete=models.CASCADE, related_name="rules")
    kind = models.CharField(max_length=64, choices=RuleKind.choices)
    config = models.JSONField(default=dict, help_text="Per-kind parameters; see registry.py")

    state = models.CharField(max_length=16, choices=RuleState.choices, default=RuleState.DRAFT)
    on_breach = models.CharField(max_length=16, choices=OnBreach.choices, default=OnBreach.REFUSE)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    first_enforced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "kind"],
                condition=~models.Q(status=Status.DELETED),
                name="uniq_policy_rule_kind_live",
            ),
        ]
        base_manager_name = "unfiltered_objects"

    def __str__(self):
        return f"{self.kind} ({self.state}) in {self.policy_id}"

    @property
    def is_enforcing(self) -> bool:
        return self.state == RuleState.ENFORCING


class StepUp(TenantScoped):
    """A purchase parked until a human approves it.

    The token in the URL is the capability -- there is no login. Standard for
    approval links and correct for a demo, but stated in the README as a
    simplification: production wants the approver authenticated.
    """
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    mandate = models.ForeignKey(Mandate, on_delete=models.CASCADE, related_name="step_ups")
    actor = models.CharField(max_length=16, help_text="buyer or merchant")

    product_url = models.URLField(max_length=1024)
    title = models.CharField(max_length=512)
    sku = models.CharField(max_length=128, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    quoted_price_paise = models.BigIntegerField()
    amount_paise = models.BigIntegerField()

    reason_code = models.CharField(max_length=64)
    detail = models.JSONField(default=dict)

    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)

    plink_id = models.CharField(max_length=64, blank=True)
    short_url = models.URLField(max_length=512, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status", "expires_at"],
                         name="idx_stepup_tenant_status_exp"),
        ]

    def __str__(self):
        return f"{self.reason_code} on {self.title[:40]} ({self.status})"

    def is_expired(self, now=None) -> bool:
        return (now or timezone.now()) >= self.expires_at

    @property
    def is_actionable(self) -> bool:
        return self.status == self.Status.PENDING and not self.is_expired()
