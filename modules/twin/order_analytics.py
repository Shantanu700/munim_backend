"""What the orders dashboard needs that a list of rows cannot answer.

Mirrors modules/policy/analytics.py in shape and placement: read-only, no
ledger appends, no writes of any kind. The difference is that these numbers are
about money and orders, where policy/analytics.py's tiles are about rules.

Everything here windows on the MERCHANT's calendar, not UTC's -- see
month_start below, and services.day_start for the same reasoning applied to a
daily cap.
"""
from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from agentmcp.clients import SIGNATURES
from modules.ledger.models import LedgerEntry
from modules.policy.models import StepUp
from modules.policy.reasons import DESCRIPTIONS, ReasonCode
from modules.twin.orders import Order

NEEDS_ATTENTION = (Order.Status.FAILED, Order.Status.EXPIRED)

UNKNOWN_AGENT = "unknown"
AGENT_VALUES = tuple(label for label, _ in SIGNATURES) + (UNKNOWN_AGENT,)

MAX_QUERY_LENGTH = 128



def month_start(now=None):
    """Midnight on the 1st of the merchant's month, not UTC's.

    Same reason services.day_start() exists: a 00:30 IST order on 1 September
    is 19:00 UTC on 31 August, and a revenue tile that files it under August is
    wrong on the first night of every month.
    """
    now = now or timezone.now()
    first = timezone.localdate(now).replace(day=1)
    return timezone.make_aware(datetime.combine(first, time.min))


def prev_month_start(now=None):
    """Midnight on the 1st of the month before. Via the current month's start
    minus a day, so month lengths and year boundaries take care of themselves.
    """
    return month_start(month_start(now) - timedelta(days=1))



def paid_months(tenant, now=None, *, include_all_time=False) -> dict:
    """This month, last month and optionally all time, in ONE query.

    Both month windows come out of one aggregate over one index range, using
    aggregate filter= -- the alternative is two round trips to compute a figure
    ("up from X last month") that only means anything as a pair.

    Money is summed in paise and divided in paise: there is no MoneyField in
    this project, every column is *_paise, and a float average of currency is a
    rounding bug waiting for a big enough month.

    `include_all_time` adds the store's whole history to that same aggregate,
    for the dashboard's payments card -- in the same query and not a second one,
    so an all-time total cannot disagree with the month it contains. Behind a
    flag because it costs the index range the month figures otherwise get, and
    /orders/tiles has no use for it.

    Caveat worth knowing: the all-time terms filter on `status` alone while the
    windowed ones filter on `paid_at`. mark_paid() and the webhook always write
    both together, so the two agree -- but a PAID row with a null paid_at (a
    hand-rolled fixture) would appear in all-time and in no month. Left visible
    rather than guarded, because a paid_at__isnull=False guard would hide the
    inconsistency instead of surfacing it.

    Split out of tiles() so the dashboard's payments card can reuse the exact
    same aggregate without also paying for tiles()' step-up count, which it does
    not display. Reuse should not cost a wasted round trip to get.
    """
    now = now or timezone.now()
    this_month, last_month = month_start(now), prev_month_start(now)

    paid = Order.objects.filter(tenant=tenant, status=Order.Status.PAID)
    if not include_all_time:
        paid = paid.filter(paid_at__gte=last_month)

    this_window = Q(paid_at__gte=this_month)
    prior_window = Q(paid_at__gte=last_month, paid_at__lt=this_month)

    aggregates = {
        "amount": Coalesce(Sum("amount_paise", filter=this_window), 0),
        "count": Count("id", filter=this_window),
        "prior_amount": Coalesce(Sum("amount_paise", filter=prior_window), 0),
        "prior_count": Count("id", filter=prior_window),
    }
    if include_all_time:
        aggregates["all_time_amount"] = Coalesce(Sum("amount_paise"), 0)
        aggregates["all_time_count"] = Count("id")

    figures = paid.aggregate(**aggregates)
    figures["this_month"] = this_month
    figures["last_month"] = last_month
    return figures


def money_card(tenant, now=None, *, include_all_time=False) -> dict:
    """The month figures as a response, shared by /orders/tiles and the
    dashboard's payments card so the two cannot report different months."""
    paid = paid_months(tenant, now=now, include_all_time=include_all_time)
    average = paid["amount"] // paid["count"] if paid["count"] else 0
    prior_average = (paid["prior_amount"] // paid["prior_count"]
                     if paid["prior_count"] else 0)

    return {
        "currency": tenant.currency,
        **({"paid_all_time": {"amount_paise": paid["all_time_amount"],
                              "count": paid["all_time_count"]}}
           if include_all_time else {}),
        "paid_prior_month": {
            "amount_paise": paid["prior_amount"],
            "count": paid["prior_count"],
            "month": paid["last_month"].strftime("%Y-%m"),
        },
        "paid_this_month": {
            "amount_paise": paid["amount"],
            "count": paid["count"],
            "month": paid["this_month"].strftime("%Y-%m"),
        },
        "average_order_value": {
            "amount_paise": average,
            "count": paid["count"],
            "prior_amount_paise": prior_average,
            "prior_month": paid["last_month"].strftime("%Y-%m"),
            "direction": _direction(average, prior_average) if paid["prior_count"] else "",
        },
    }



def tiles(tenant, now=None, *, include_all_time=False) -> dict:
    """The three KPI tiles, in two queries: money_card's, plus the step-ups."""
    now = now or timezone.now()
    card = money_card(tenant, now=now, include_all_time=include_all_time)

    awaiting = StepUp.objects.filter(
        tenant=tenant, status=StepUp.Status.PENDING, expires_at__gt=now,
    ).aggregate(amount=Coalesce(Sum("amount_paise"), 0), count=Count("id"))

    return {
        **card,
        "awaiting_approval": {
            "amount_paise": awaiting["amount"],
            "count": awaiting["count"],
        },
    }


def _direction(current: int, prior: int) -> str:
    if current > prior:
        return "up"
    return "down" if current < prior else "flat"



def counts(tenant, now=None, queryset=None) -> dict:
    """The tab bar, in two queries.

    `queryset` lets the caller hand in an already-filtered set so the tabs
    agree with the list under an active search. It must NOT be filtered by
    status -- this function is the status breakdown.
    """
    now = now or timezone.now()
    orders = queryset if queryset is not None else Order.objects.filter(tenant=tenant)

    by_status = dict(orders.values_list("status").annotate(n=Count("id")))
    tally = {value: by_status.get(value, 0) for value in Order.Status.values}
    total = sum(tally.values())

    step_ups = StepUp.objects.filter(
        tenant=tenant, status=StepUp.Status.PENDING, expires_at__gt=now).count()

    return {
        "orders_total": total,
        "by_status": tally,
        "tabs": [
            {"key": "all", "label": "All", "count": total, "source": "orders"},
            {"key": Order.Status.PAID, "label": "Paid",
             "count": tally[Order.Status.PAID], "source": "orders"},
            {"key": Order.Status.PENDING, "label": "Awaiting payment",
             "count": tally[Order.Status.PENDING], "source": "orders"},
            {"key": "awaiting", "label": "Awaiting approval",
             "count": step_ups, "source": "step_ups"},
            {"key": "needs_attention", "label": "Needs attention",
             "count": sum(tally[s] for s in NEEDS_ATTENTION), "source": "orders"},
        ],
    }



def reason_codes_matching(q: str) -> set[str]:
    """Reason codes whose name or merchant-facing sentence contains `q`.

    Resolved against the static tables in policy/reasons.py, so this costs no
    query at all -- "budget", "cap", "price changed" and BUDGET_EXCEEDED itself
    all land on the same codes.
    """
    upper, lower = q.upper(), q.lower()
    named = {code for code in ReasonCode.values if upper in code}
    described = {code for code, text in DESCRIPTIONS.items() if lower in text.lower()}
    return named | described


def search_q(tenant, q: str) -> Q:
    """The search box's promise -- "orders, products, reason codes" -- as a Q.

    Reason codes reach orders by three routes, and the ledger one is the
    weakest: an Order exists only on ALLOW, so its own ledger entry's
    reason_code is always ALLOWED. The two that earn their keep are
    step_up__reason_code -- "which orders went through after a BUDGET_EXCEEDED
    approval" -- and failure_reason_code, which is what makes searching
    "expired" return the expired orders rather than nothing. Both are columns
    reachable without a JSON scan, which is the point of storing them.

    ponytail: icontains on title/sku is a sequential scan. Correct at this
    scale and a settings change away from better -- django.contrib.postgres is
    not in INSTALLED_APPS, so SearchVector and TrigramSimilarity are not
    available today. The upgrade is pg_trgm plus a GIN index on (title, sku).
    """
    q = q.strip()[:MAX_QUERY_LENGTH]
    number = q.upper().removeprefix("#").removeprefix("MNM-")

    clause = (
        Q(order_number__icontains=number)
        | Q(title__icontains=q)
        | Q(sku__icontains=q)
        | Q(product_url__icontains=q)
        | Q(buyer__email__icontains=q)
        | Q(buyer__name__icontains=q)
        | Q(plink_id__iexact=q)
        | Q(reference_id__iexact=q)
    )

    if codes := reason_codes_matching(q):
        clause |= Q(step_up__reason_code__in=codes)
        clause |= Q(failure_reason_code__in=codes)
        clause |= Q(ledger_seq__in=LedgerEntry.objects.filter(
            tenant=tenant, reason_code__in=codes).values("seq"))

    return clause


def buyer_search_q(q: str) -> Q:
    """The same search box, over the BUYER's own history.

    Not search_q() with the tenant argument dropped. That one reaches buyer
    email and name, which on this surface are one buyer and so match either
    every row or none, and plink_id and reference_id, which are the merchant's
    Razorpay handles -- both are on buyer_order_serializers.py's never-exposed
    list, and a field the buyer is never shown is not one they search by.

    What it adds instead is the merchant's domain, because the buyer's list is
    the one screen in the product that spans stores.

    Of the three routes from a reason code to an order, only failure_reason_code
    survives the crossing. A step-up reason and a ledger entry are the
    merchant's record of how the decision got made; this endpoint does not show
    them, and searching by something invisible returns rows the reader cannot
    explain.

    Takes no tenant, and must not: the caller scopes to the buyer, and every
    clause here is a column on Order or on its own tenant row.
    """
    q = q.strip()[:MAX_QUERY_LENGTH]
    number = q.upper().removeprefix("#").removeprefix("MNM-")

    clause = (
        Q(order_number__icontains=number)
        | Q(title__icontains=q)
        | Q(sku__icontains=q)
        | Q(product_url__icontains=q)
        | Q(tenant__domain__icontains=q)
    )

    if codes := reason_codes_matching(q):
        clause |= Q(failure_reason_code__in=codes)

    return clause


def payment_search_q(q: str) -> Q:
    """The same search box, over the merchant's PAYMENT ATTEMPTS.

    Not search_q() with `order__` pasted in front of every lookup. Two of its
    clauses would land on the wrong row: plink_id and reference_id exist on both
    models, and Order's are the projection of whichever attempt reproject() last
    promoted -- so searching a link handle through the order would match every
    attempt of that order, including the ones that never carried it. The whole
    reason this endpoint exists is to tell those attempts apart, so both are
    read off the Payment row here.

    Its ledger and step-up clauses are dropped for the reason buyer_search_q
    drops them: they are facts about how the ORDER was decided, and an order
    that took three tries was decided once. Order.ledger_seq is that decision;
    Payment.ledger_seq is the checkout that authorised one retry, and searching
    a reason code has never meant "find the retry it authorised".

    Buyer email and name stay, unlike buyer_search_q -- this list spans the
    store's buyers, so they discriminate here where on the buyer's own screen
    they match either everything or nothing.

    Takes no tenant, and needs none: every clause is a column on Payment or on
    the Order it hangs off, and the caller has already scoped to the store. The
    icontains-is-a-sequential-scan caveat on search_q applies here too.
    """
    q = q.strip()[:MAX_QUERY_LENGTH]
    number = q.upper().removeprefix("#").removeprefix("MNM-")

    clause = (
        Q(order__order_number__icontains=number)
        | Q(order__title__icontains=q)
        | Q(order__sku__icontains=q)
        | Q(order__buyer__email__icontains=q)
        | Q(order__buyer__name__icontains=q)
        | Q(plink_id__iexact=q)
        | Q(reference_id__iexact=q)
    )

    if codes := reason_codes_matching(q):
        clause |= Q(failure_reason_code__in=codes)

    return clause
