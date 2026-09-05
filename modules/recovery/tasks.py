from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from modules.policy.reasons import ReasonCode
from modules.twin.orders import Order
from modules.twin.payments import Payment, reproject


@shared_task
def ping(value="pong"):
    """Proves broker -> worker round trip. Delete once real tasks exist."""
    return value


EXPIRY_GRACE_MINUTES = 30


@shared_task
def expire_stale_orders():
    """Flip payment links past their life to EXPIRED, then re-derive their orders.

    Order.Status.EXPIRED existed from the start and nothing ever set it, which
    was harmless while an order's status was only ever read for display. It
    stops being harmless the moment a daily spend cap counts authorised orders:
    an agent that opens links and never pays would otherwise accumulate PENDING
    rows forever, and the cap -- reading them as spend -- would lock the
    merchant out of their own store a little more each day.

    Counting only PAID instead is not the alternative it looks like. Payment is
    asynchronous, so nothing is PAID at the moment the gate has to decide, and
    an agent could open any number of links at once and settle them later. The
    cap would never bind. Authorised-and-not-expired is the only window that
    both binds and drains.

    A FALLBACK, since payment_link.expired was wired up in the webhook handler.
    That event is Razorpay's own verdict and arrives sooner, and this task's
    PENDING filter means it simply finds nothing once the webhook has acted.
    The task stays because webhook subscription is configured by each merchant
    in their own Razorpay dashboard, so a store that never subscribed still
    needs its abandoned links drained or the daily cap ratchets shut on them.

    The unit of expiry is the LINK, not the order. A retried order is old while
    its second link is minutes fresh, so keying on Order.authorized_at would
    reap a live link; Payment.expires_at is absolute and per-attempt, which is
    why it is stored rather than derived from the order's age.

    Returns (failed, expired), counted in PAYMENTS. For an order that was never
    retried -- every order that existed before this split -- that is the same
    number it always was.
    """
    cutoff = timezone.now()
    stale = Payment.objects.filter(status=Payment.Status.PENDING,
                                   expires_at__lt=cutoff - timedelta(
                                       minutes=EXPIRY_GRACE_MINUTES))

    order_ids = list(stale.values_list("order_id", flat=True).distinct())

    now = timezone.now()
    failed = stale.filter(failed_attempts__gt=0).update(
        status=Payment.Status.FAILED, closed_at=now,
        failure_reason_code=ReasonCode.PAYMENT_ATTEMPT_FAILED)
    expired = stale.filter(failed_attempts=0).update(
        status=Payment.Status.EXPIRED, closed_at=now,
        failure_reason_code=ReasonCode.LINK_EXPIRED)

    for order in Order.objects.filter(id__in=order_ids):
        reproject(order)
    return failed, expired
