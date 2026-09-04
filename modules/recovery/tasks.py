from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from modules.policy.reasons import ReasonCode
from modules.twin.orders import Order


@shared_task
def ping(value="pong"):
    """Proves broker -> worker round trip. Delete once real tasks exist."""
    return value


EXPIRY_GRACE_MINUTES = 30


@shared_task
def expire_stale_orders():
    """Flip PENDING orders past their link's life to EXPIRED.

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

    Returns (failed, expired).
    """
    from agentmcp.checkout import LINK_MINUTES

    cutoff = timezone.now() - timedelta(minutes=LINK_MINUTES + EXPIRY_GRACE_MINUTES)
    stale = Order.objects.filter(status=Order.Status.PENDING, authorized_at__lt=cutoff)

    failed = stale.filter(failed_attempts__gt=0).update(
        status=Order.Status.FAILED,
        failure_reason_code=ReasonCode.PAYMENT_ATTEMPT_FAILED)
    expired = stale.filter(failed_attempts=0).update(
        status=Order.Status.EXPIRED, failure_reason_code=ReasonCode.LINK_EXPIRED)
    return failed, expired
