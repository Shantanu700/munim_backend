"""One Razorpay Payment Link attempt, and the projection back onto its order.

Order was welded to a single link: it carried plink_id, short_url and paid_at
directly, so "retry" could only mean "call checkout again", which minted a whole
new Order with a new order number. One purchase intent became N orders, the
buyer's history became unreadable, and -- worse -- day_spent_paise counts
PENDING orders, so every abandoned attempt ate the buyer's daily cap until the
reaper drained it an hour later.

Splitting the two fixes all three at once. Order is the INTENT: what the buyer
agreed to buy, at what price, under which mandate. Payment is one TRY at
collecting for it. A retry adds a row here, so the order number is stable and
the cap counts the intent once however many links it took.

Order keeps its payment columns as a denormalised projection of the latest
meaningful attempt -- see reproject() below, which is the only thing allowed to
write them after creation. Every merchant dashboard, analytics query and
serializer reads those columns and none of them had to change.
"""
from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

import uuid6

from modules.core.models import TenantScoped
from modules.twin.orders import Order

PAYMENT_ATTEMPT_TRIES = 3


class PaymentAlreadyLive(Exception):
    """This order already has an unexpired link. A second one is a double charge."""

    def __init__(self, order_number: str):
        self.order_number = order_number
        super().__init__(f"Order {order_number} already has a live payment link.")


class Payment(TenantScoped):
    """One Razorpay Payment Link attempt against one Order.

    `status` shadows ModelBase.status (an IntegerField soft-delete flag) exactly
    as Order.status does. DeleteStatusManager's exclude(status=DELETED) then
    compares a CharField against "2" and matches nothing, so this table -- like
    orders -- is not soft-delete filtered. Deliberate: a payment attempt is a
    fact about what happened and is never retracted.

    `requested_at` rather than the inherited created_at for the same reason
    Order has authorized_at: ModelBase declares created_at as auto_now and
    updated_at as auto_now_add, which is the wrong way round, so created_at
    moves on every save. "When was this link minted" must not.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Link cancelled or declined"
        EXPIRED = "expired", "Link expired"

    CLOSED = (Status.PAID, Status.FAILED, Status.EXPIRED)

    uuid = models.UUIDField(default=uuid6.uuid6, unique=True, editable=False)

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")

    attempt = models.PositiveSmallIntegerField()

    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)

    plink_id = models.CharField(max_length=64, blank=True, db_index=True)
    short_url = models.URLField(max_length=512, blank=True)
    reference_id = models.CharField(max_length=64, blank=True, db_index=True)

    amount_paise = models.BigIntegerField()
    currency = models.CharField(max_length=3, default="INR")

    ledger_seq = models.BigIntegerField(null=True, blank=True)

    requested_at = models.DateTimeField(default=timezone.now, editable=False,
                                        db_index=True)
    expires_at = models.DateTimeField(db_index=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    amount_paid_paise = models.BigIntegerField(null=True, blank=True)

    failed_attempts = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    failure_reason_code = models.CharField(max_length=64, blank=True)

    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order_id", "attempt"]
        constraints = [
            models.UniqueConstraint(fields=["order", "attempt"],
                                    name="uniq_payment_attempt_per_order"),
            models.UniqueConstraint(fields=["order"],
                                    condition=models.Q(status="pending"),
                                    name="uniq_live_payment_per_order"),
            models.UniqueConstraint(fields=["tenant", "reference_id"],
                                    condition=~models.Q(reference_id=""),
                                    name="uniq_payment_reference_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "plink_id"],
                                    condition=~models.Q(plink_id=""),
                                    name="uniq_payment_plink_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "status", "expires_at"],
                         name="idx_payment_tenant_status_exp"),
            models.Index(fields=["order", "-attempt"], name="idx_payment_order_attempt"),
            models.Index(fields=["tenant", "requested_at"], name="idx_payment_tenant_req"),
            models.Index(fields=["tenant", "-requested_at", "-id"],
                         name="idx_payment_tenant_req_id"),
        ]

    def __str__(self):
        return f"attempt {self.attempt} of {self.order_id} ({self.status})"

    @property
    def rupees(self) -> str:
        return f"{self.amount_paise / 100:,.2f}"

    def is_live(self, now=None) -> bool:
        """Still payable: pending, and the link has not lapsed."""
        return (self.status == self.Status.PENDING
                and self.expires_at > (now or timezone.now()))


def create_payment(order, *, plink_id, short_url, reference_id, amount_paise,
                   expires_at, currency="INR", ledger_seq=None) -> Payment:
    """The only correct way to insert a Payment. Mirrors orders.create_order().

    Locks the ORDER row, not the Merchant row. The counter being allocated is
    per-order, and taking the store's row here would serialise every checkout in
    the store behind every other one -- which is exactly what allocate_order_
    number already has to do, and doing it twice would be worse.

    The (order, attempt) unique index is the backstop, not the mechanism, the
    same way allocate_order_number describes its own. Hence the retry loop.

    Deliberately appends NOTHING to the ledger. LedgerEntry.append takes the
    Merchant row lock; taking it while holding an Order lock inverts the order
    the webhook path uses (update payment, then reproject under the Order lock,
    then append after) and that is a deadlock. The rule for this whole feature:
    NEVER hold the Order row lock across a ledger append.

    Callers must have created the Payment Link on Razorpay BEFORE calling this,
    for the reason create_order's own docstring gives -- a network side effect no
    rollback can undo must not sit inside a row lock.
    """
    for attempt_no in range(1, PAYMENT_ATTEMPT_TRIES + 1):
        try:
            with transaction.atomic():
                locked = Order.objects.select_for_update().get(pk=order.pk)
                live = locked.payments.filter(status=Payment.Status.PENDING).first()
                if live is not None:
                    raise PaymentAlreadyLive(locked.order_number)

                payment = Payment.objects.create(
                    tenant_id=locked.tenant_id, order=locked,
                    attempt=locked.attempts + 1,
                    plink_id=plink_id, short_url=short_url,
                    reference_id=reference_id, amount_paise=amount_paise,
                    currency=currency, ledger_seq=ledger_seq,
                    expires_at=expires_at,
                )
                Order.objects.filter(pk=locked.pk).update(
                    attempts=F("attempts") + 1,
                    status=Order.Status.PENDING,
                    plink_id=plink_id, short_url=short_url,
                    reference_id=reference_id, failure_reason_code="",
                )
                order.refresh_from_db()
                return payment
        except IntegrityError:
            if attempt_no == PAYMENT_ATTEMPT_TRIES:
                raise


def reproject(order) -> None:
    """Rewrite Order's denormalised columns from its payments.

    The ONLY writer of Order.status outside creation. Called after every Payment
    transition -- from the webhook handlers and from the reaper alike.

    Precedence, and this ordering IS the correctness argument:

      1. any payment PAID    -> order PAID     (a sale is never undone)
      2. any payment PENDING -> order PENDING  (a live link is still payable)
      3. otherwise           -> the LATEST attempt's terminal status

    Rules 1 and 2 are the guard for the sequence Razorpay does not order for us.
    An `expired` for attempt 1 arriving AFTER attempt 2 was paid recomputes to
    PAID under rule 1; one arriving while attempt 2 is still live recomputes to
    PENDING under rule 2. Neither can reach the terminal branch, because that
    branch is only reachable when nothing is paid and nothing is live. Attempt
    1's own row still records that it expired -- which is true, and is the fact
    the old single-link Order had nowhere to put.

    The alternative shape, "ignore an event for an attempt that is not the
    latest", is worse twice over: it discards a true fact about attempt 1, and
    it breaks entirely on an out-of-order redelivery.

    select_for_update is load-bearing, not decoration. Two deliveries for two
    attempts of one order can land in two worker processes at once, and an
    unlocked read-modify-write lets the older snapshot win.

    failed_attempts and last_failure_at are NOT recomputed here. They are
    cumulative across attempts and are incremented in place by
    _record_failed_attempt; recomputing them would put a SUM on every webhook.
    """
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        rows = list(locked.payments.order_by("attempt"))
        if not rows:
            return

        paid = next((p for p in rows if p.status == Payment.Status.PAID), None)
        live = next((p for p in rows if p.status == Payment.Status.PENDING), None)
        latest = rows[-1]
        face = paid or live or latest

        Order.objects.filter(pk=locked.pk).update(
            status=face.status,
            plink_id=face.plink_id,
            short_url=face.short_url,
            reference_id=face.reference_id,
            paid_at=paid.paid_at if paid else None,
            failure_reason_code=("" if (paid or live)
                                 else latest.failure_reason_code or ""),
        )
