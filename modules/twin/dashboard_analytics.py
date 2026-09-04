"""What the merchant's home dashboard needs, assembled once per screen.

Sibling of order_analytics.py, and it inherits that module's three rules:
read-only, no ledger appends, and every window on the MERCHANT's calendar
rather than UTC's -- see order_analytics.month_start and
policy.services.day_start for the reasoning.

Reuse over re-derivation is the organising principle here. The order status
counts come straight from order_analytics.counts(), the money months from
order_analytics.tiles(), and the store-policy panel from
policy.analytics.store_card(). Every one of those is the single definition
shared with a surface that already exists, and a dashboard number that
disagrees with the screen it links to is worse than no number.

Two figures on the design cannot be served and are not faked. Neither the
Razorpay `acc_...` settlement id (permanently None -- payments/razorpay.py
says why) nor a "2.1x your storefront average" ratio (there is no storefront
denominator: every Order row is an agent purchase) has data behind it. Both are
returned as an explicit null next to a server-composed sentence.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import reduce
from operator import add, and_

from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Length, TruncDate
from django.utils import timezone

from modules.core.services import mask_key_id
from modules.policy.registry import PRODUCT_DATA_FIELDS
from modules.twin import order_analytics
from modules.twin.choices import CatalogReadTool
from modules.twin.models import CatalogRead, Product
from modules.twin.order_analytics import AGENT_VALUES, UNKNOWN_AGENT
from modules.twin.orders import Order

READ_WINDOW_DAYS = 7
REVENUE_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 365



def window(days: int, now=None) -> tuple[datetime, datetime, list]:
    """A window as (start, end, spine) on the MERCHANT's calendar.

    `start` is midnight local on the first day and `end` midnight local on the
    day AFTER the last -- half open, so today is whole and a paid_at from a
    skewed clock cannot land in a bucket the caller did not ask for.

    `spine` is every calendar date in the range, in order. It is the zero-fill
    template: a series always has exactly `days` points whether or not anything
    happened, so a chart cannot silently skip a date and a client cannot have to
    guess which day a gap belongs to.
    """
    now = now or timezone.now()
    last = timezone.localdate(now)
    first = last - timedelta(days=days - 1)
    start = timezone.make_aware(datetime.combine(first, time.min))
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min))
    spine = [first + timedelta(days=i) for i in range(days)]
    return start, end, spine


def clamp_days(value, default: int, maximum: int = MAX_WINDOW_DAYS) -> int:
    """A window length from a query string. Clamped, never refused.

    Unlike ?status= and ?agent=, which name things that either exist or do not,
    a window is a number and every number has a nearest legal value. Garbage
    falls back to the default rather than 400ing a whole dashboard over one
    mistyped chart control.
    """
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(days, maximum))


def _fold_agent(label: str, buckets: dict) -> str:
    """The wire name for a stored llm_client / agent_label.

    "" becomes "unknown", matching OrderSerializer.get_agent_label, so the two
    surfaces agree about what an unattributed agent is called.

    A label that is not a bucket -- a SIGNATURES entry renamed or removed while
    its rows remain -- folds into "unknown" rather than being appended or
    dropped. Appended is wrong because every bar links to /orders/?agent=<key>
    and that filter accepts only AGENT_VALUES, so a new bar would open a 400.
    Dropped is worse: the bars would quietly stop summing to the line, which is
    the first thing a merchant checks.
    """
    label = label or UNKNOWN_AGENT
    return label if label in buckets else UNKNOWN_AGENT


def _share_bps(part: int, whole: int) -> int:
    """Basis points, as an integer. A client that wants "57.1%" divides by 100.

    Integers end to end, like every other figure here: a float share of a paise
    total invites the same rounding drift tiles() refuses for money.
    """
    return part * 10_000 // whole if whole else 0



SETTLEMENT_NOT_READY = "Not connected yet -- finish the Razorpay step to take payments."
SETTLEMENT_TEST_MODE = "Test mode -- no real money has settled."


def payments(tenant, now=None) -> dict:
    """Money received, all time, with the month either side of it for context.

    One query. order_analytics.money_card computes the all-time total in the
    SAME aggregate as the two months, so the headline cannot disagree with the
    month it contains -- and it is the same function /orders/tiles reads, so the
    two screens cannot disagree about a month either.

    money_card and not tiles: tiles adds a step-up count this card does not
    display, and reuse should not cost a wasted round trip to get.
    """
    now = now or timezone.now()
    tile = order_analytics.money_card(tenant, now=now, include_all_time=True)

    return {
        "currency": tile["currency"],
        "all_time": tile["paid_all_time"],
        "this_month": tile["paid_this_month"],
        "prior_month": tile["paid_prior_month"],
        "direction": tile["average_order_value"]["direction"],
        "settlement": settlement(tenant),
    }


def settlement(tenant) -> dict:
    """Where the money actually lands, and one sentence saying so.

    account_id is always None and that is not a bug: verify_credentials cannot
    fill it, because the Razorpay Partner accounts API that would is for
    sub-merchants and Munim has none. The design's "acc_QK3n...4471" therefore
    has nothing behind it, and inventing one would be a fabricated banking
    reference on a page a merchant reads to reassure themselves about money.
    The masked key id is the real, checkable reference.

    Masked rather than raw even though /core/razorpay already returns the raw
    key id to this same authenticated merchant. That is not a security boundary
    -- it is a dashboard, and a dashboard gets screenshared.

    `label` is composed here rather than in the client for the reason
    order_analytics._direction is: the sentence and the numbers printed beside
    it must not be able to drift apart.
    """
    key_id = tenant.razorpay_key_id
    masked = mask_key_id(key_id) if key_id else None
    mode = tenant.razorpay_key_mode

    if not tenant.is_payment_ready:
        label = SETTLEMENT_NOT_READY
    elif mode == "test":
        label = SETTLEMENT_TEST_MODE
    else:
        label = f"Settled straight to your own Razorpay account ({masked})"

    return {
        "is_payment_ready": tenant.is_payment_ready,
        "account_id": tenant.razorpay_account_id or None,
        "account_name": tenant.razorpay_account_name or None,
        "key_id_masked": masked,
        "key_mode": mode,
        "business_name": tenant.business_name or None,
        "domain": tenant.domain or None,
        "label": label,
    }



def orders(tenant, now=None) -> dict:
    """The orders card: total, per-status, and the tab bar's own labels.

    Straight through to order_analytics.counts(). It exists as its own function
    so the dashboard imports one module rather than two, and so a
    dashboard-only tweak has an obvious home that is not the function the tab
    bar shares.

    Re-deriving "4 paid, 3 need attention" here instead would fork
    NEEDS_ATTENTION, which is the single definition the ?status= filter also
    reads -- and a card that disagrees with the list it opens is exactly what
    that constant exists to prevent.
    """
    return order_analytics.counts(tenant, now=now)



COMPARISON_UNAVAILABLE = "unavailable"
COMPARISON_NOTE = (
    "Munim only sees purchases agents made, so there is no storefront average "
    "to compare an agent basket against."
)
ATTRIBUTION_CAVEAT = (
    "Which agent placed an order is read from the caller's User-Agent and "
    "Origin. It is what the client reported, not an authenticated identity, and "
    "it never influenced the decision to allow the purchase."
)


def revenue_and_agents(tenant, days: int = REVENUE_WINDOW_DAYS, now=None) -> dict:
    """The revenue chart, the per-agent bars and the basket average. One query.

    They are one aggregate because they are one set of facts: the bars have to
    sum to the line, and the basket average has to be that line's total over its
    own count. Two GROUP BYs would let a merchant find three numbers on one
    screen that cannot all be true.

    Zero-filled in Python over `window`'s spine rather than in SQL. Filling in
    SQL needs a generate_series join Django cannot express without RawSQL, buys
    nothing at 14 rows, and would put a hand-written date series outside the
    ORM's timezone handling -- the one part of this least worth hand-writing.
    The client still receives a complete series, and the fill is testable with
    no database.
    """
    now = now or timezone.now()
    start, end, spine = window(days, now)
    tz = timezone.get_current_timezone()

    rows = list(
        Order.objects.filter(
            tenant=tenant, status=Order.Status.PAID,
            paid_at__gte=start, paid_at__lt=end,
        )
        .annotate(day=TruncDate("paid_at", tzinfo=tz))
        .values("day", "llm_client")
        .annotate(amount_paise=Coalesce(Sum("amount_paise"), 0), count=Count("id"))
    )

    by_day = {day: {"amount_paise": 0, "count": 0} for day in spine}
    by_agent = {label: {"amount_paise": 0, "count": 0} for label in AGENT_VALUES}
    total = {"amount_paise": 0, "count": 0}

    for row in rows:
        for bucket in (by_day.get(row["day"]),
                       by_agent[_fold_agent(row["llm_client"], by_agent)],
                       total):
            if bucket is None:
                continue
            bucket["amount_paise"] += row["amount_paise"]
            bucket["count"] += row["count"]

    return {
        "window": {"days": days, "start": spine[0], "end": spine[-1]},
        "currency": tenant.currency,
        "series": [{"day": day, **by_day[day]} for day in spine],
        "total": total,
        "agents": [
            {"agent": label, "amount_paise": bucket["amount_paise"],
             "count": bucket["count"],
             "share_bps": _share_bps(bucket["amount_paise"], total["amount_paise"])}
            for label, bucket in by_agent.items()
        ],
        "average_basket_paise": (total["amount_paise"] // total["count"]
                                 if total["count"] else 0),
        "storefront_comparison": None,
        "comparison_basis": COMPARISON_UNAVAILABLE,
        "comparison_note": COMPARISON_NOTE,
        "attribution_caveat": ATTRIBUTION_CAVEAT,
    }


def basket_comparison(tenant, days: int = REVENUE_WINDOW_DAYS, now=None) -> dict:
    """The basket average over the window before this one, and which way it moved.

    The comparison the merchant CAN act on, in place of the storefront ratio
    that has no denominator. "Up from 5,900 last fortnight" is a true sentence
    about their own store; "2.1x your storefront average" is not a sentence this
    system has the facts to write.
    """
    now = now or timezone.now()
    start, _end, _spine = window(days, now)
    prior_start, _e, _s = window(days, start - timedelta(seconds=1))

    prior = Order.objects.filter(
        tenant=tenant, status=Order.Status.PAID,
        paid_at__gte=prior_start, paid_at__lt=start,
    ).aggregate(amount=Coalesce(Sum("amount_paise"), 0), count=Count("id"))

    average = prior["amount"] // prior["count"] if prior["count"] else 0
    return {
        "days": days,
        "amount_paise": prior["amount"],
        "count": prior["count"],
        "average_basket_paise": average,
    }



def catalog_reads(tenant, days: int = READ_WINDOW_DAYS, now=None) -> dict:
    """How often agents read this store's catalogue, and which agents.

    One query and no TruncDate: CatalogRead.day is already a DateField on the
    merchant's calendar, which is the entire reason it is stored that way rather
    than as a timestamp. The result set is bounded by
    days x len(AGENT_VALUES) x len(CatalogReadTool), so the three rollups are
    done in Python over rows already fetched.

    unfiltered_objects, matching CatalogRead.record(). The counter and the card
    must not disagree about which rows exist.
    """
    now = now or timezone.now()
    _start, _end, spine = window(days, now)

    rows = CatalogRead.unfiltered_objects.filter(
        tenant=tenant, day__gte=spine[0], day__lte=spine[-1],
    ).values_list("day", "agent_label", "tool", "count")

    by_day = {day: 0 for day in spine}
    by_agent = {label: 0 for label in AGENT_VALUES}
    by_tool = {tool: 0 for tool in CatalogReadTool.values}
    total = 0

    for day, agent_label, tool, count in rows:
        by_day[day] = by_day.get(day, 0) + count
        by_agent[_fold_agent(agent_label, by_agent)] += count
        by_tool[tool] = by_tool.get(tool, 0) + count
        total += count

    return {
        "window": {"days": days, "start": spine[0], "end": spine[-1]},
        "total": total,
        "by_day": [{"day": day, "count": by_day[day]} for day in spine],
        "by_agent": [{"agent": label, "count": count} for label, count in by_agent.items()],
        "by_tool": by_tool,
    }



PRESENT: dict[str, Q] = {
    "title": Q(title__regex=r"\S"),
    "price_paise": Q(price_paise__gt=0),
    "currency": Q(currency_len=3),
    "sku": Q(sku__regex=r"\S"),
    "image_url": Q(image_url__regex=r"\S"),
    "in_stock": Q(in_stock__isnull=False),
}

MISSING_ANY = "any"

ALL_PRESENT: Q = reduce(and_, PRESENT.values())

SELLABLE_CAVEAT = (
    "This counts what each catalogue row itself says. Stock and price are "
    "re-read from your live product page at checkout, so a row counted here can "
    "still be refused as out of stock or at a changed price."
)


def visible_products(tenant):
    """One store's agent-visible rows, plus the one expression a column cannot give.

    Product.objects is a DeleteStatusManager, so soft-deleted rows are already
    excluded -- the same set catalog.search() serves an agent from.

    Public because the ?missing= drilldown filters this same queryset with the
    same PRESENT clauses: the rows it lists and the number products() prints
    must come from one definition, or the arrow opens a different set than the
    card counted.

    ponytail: the category rules are not folded into `sellable` yet, and this is
    where they would go. There is no category column -- engine.py and
    ProductOut.of both read raw["category"] -- so it needs an annotation:

        category_key=Coalesce(Lower(KeyTextTransform("category", "raw")),
                              Value(""), output_field=TextField())

    The output_field is not optional. Without it Django raises "Expression
    contains mixed types: TextField, CharField" the moment anything SELECTs the
    annotation, which the counting aggregates happen not to do and this
    drilldown does -- so it would ship looking fine and 500 on one route.
    Written out rather than annotated-and-unused for that reason.
    """
    return Product.objects.filter(tenant=tenant, is_visible_to_agents=True).annotate(
        currency_len=Length("currency"),
    )


def products(tenant, required_fields=None, now=None) -> dict:
    """Sellable vs total, and what the gap is waiting on.

    `required_fields` is passed IN, from policy.analytics.store_card, and is not
    re-decided here: what "the six details" means is a decoding of
    PolicyRule.config that only the policy app should own. None falls back to
    every known field, for a caller with no policy context.

    The "48 / 52" figure and the "4 waiting on one detail each" figure are
    returned together, from one pair of queries, because they are usually the
    same four rows: a crawl that could not read stock leaves in_stock NULL,
    which both hides the row from search AND fails the in_stock presence check.
    Computing 52 - 48 in one card and a histogram in another would make the two
    disagree the first time a product genuinely sells out.

    What this cannot claim is "will sell". engine.evaluate checks the stock and
    price re-read from the merchant's own page at decision time; Product.in_stock
    only hides rows from the catalogue and serves as a presence check. Hence
    `caveat`, which is part of the response and not a comment.

    ponytail: two queries, and both are effectively sequential scans over the
    store's catalogue. idx_product_tenant_visible narrows the row set by index,
    but every predicate above is an unindexable per-row expression. Free at 52
    rows and not at 200k. The real fix is a maintained missing_fields column,
    written on ingest and on ProductDetailAV.put, turning both into indexed
    counts.
    """
    fields = list(PRODUCT_DATA_FIELDS if required_fields is None else required_fields)
    fields = [f for f in fields if f in PRESENT]
    visible = visible_products(tenant)

    required_q = reduce(and_, (PRESENT[f] for f in fields), Q())

    agg = visible.aggregate(
        visible=Count("id"),
        discoverable=Count("id", filter=Q(in_stock=True)),
        complete=Count("id", filter=required_q),
        sellable=Count("id", filter=Q(in_stock=True) & required_q),
        **{f"has_{field}": Count("id", filter=clause) for field, clause in PRESENT.items()},
    )

    missing_expr = reduce(add, (
        Case(When(PRESENT[f], then=Value(0)), default=Value(1), output_field=IntegerField())
        for f in fields
    ), Value(0))
    histogram = dict(
        visible.annotate(missing=missing_expr)
        .values_list("missing").annotate(n=Count("id")).order_by("missing")
    )

    return {
        "visible": agg["visible"],
        "discoverable": agg["discoverable"],
        "complete": agg["complete"],
        "sellable": agg["sellable"],
        "required_fields": fields,
        "required_count": len(fields),
        "ready_bps": _share_bps(agg["sellable"], agg["visible"]),
        "missing_by_field": {
            field: agg["visible"] - agg[f"has_{field}"] for field in PRESENT
        },
        "missing_histogram": {str(n): count for n, count in histogram.items()},
        "waiting_on_one_detail": histogram.get(1, 0),
        "caveat": SELLABLE_CAVEAT,
    }
