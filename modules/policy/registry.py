"""The rules a merchant can turn on, and the arithmetic each one performs.

Two things here are load-bearing and easy to undo by accident.

**A checker returns a breach, never a verdict.** It answers "was this rule
broken", and the merchant's `on_breach` on the row decides whether that is a
refusal or a request for a human. That split is what lets every checker be
tested with a dict and no database, and it is why the reason codes in
reasons.py no longer imply severity.

**The engine iterates this registry, not the merchant's rows.** Rule order is
therefore fixed by `phase` and `order` here, and no sequence of clicks in the
UI can change which of two simultaneous breaches is reported. A kind with no
row simply does not run, which is also what makes it safe to add a kind later
without backfilling every existing policy first.

Nothing in this module imports models. `RuleContext` carries copies of what a
checker needs, so a checker cannot lazily walk a relation into a query in the
middle of the money path.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any

from modules.policy.choices import OnBreach, RuleKind
from modules.policy.reasons import ReasonCode

log = logging.getLogger(__name__)

Breach = tuple[str, dict] | None


class Phase(IntEnum):
    """Where in the engine's sequence a rule runs.

    The engine's check order is deliberate and predates rules -- authenticity,
    then lifecycle, then the store, then the live page, then budget. Rules are
    spliced into that sequence at these four points rather than swept into one
    loop at the end, because collapsing them would change which refusal a
    caller sees first, and several of those orderings are themselves tested.
    """

    AGENT = 10
    ITEM = 20
    MONEY = 30
    TRUST = 40


@dataclass(frozen=True)
class RuleContext:
    """Everything a checker may read. Values, not model instances.

    `amount_paise` is None before Phase.MONEY, because the amount is not known
    until the live price has been read and accepted. A checker in an earlier
    phase that touches it is a bug in that checker's phase, not in the caller.
    """

    claims: Mapping[str, Any]
    product: Any
    category: str
    live_price_paise: int
    quoted_price_paise: int
    quote_from_caller: bool
    amount_paise: int | None
    day_spent_paise: int
    now: datetime


RuleFn = Callable[[Mapping[str, Any], RuleContext], Breach]


@dataclass(frozen=True)
class RuleSpec:
    kind: str
    phase: Phase
    order: int
    reason_code: str
    default_on_breach: str
    default_config: dict
    check: RuleFn
    label: str
    description: str



def _int_or_none(value) -> int | None:
    """Merchant JSON is arbitrary. A blank field arrives as "" or None, and both
    mean "unset" rather than zero -- a cap of zero would refuse everything."""
    if value is None or value == "":
        return None
    return int(value)


def agent_allowlist(config, ctx: RuleContext) -> Breach:
    """Only named agents may buy.

    Matched against the mandate's signed `sub` claim, never the User-Agent the
    caller reported -- that is spoofable and the MCP server says so explicitly.
    Worth being honest about the strength of this even so: jws.verify checks
    the signature but validates no claims, and `sub` is chosen by whoever
    issued the mandate. This is an allowlist of issuer-asserted names, not of
    authenticated agent identities.
    """
    allowed = [str(a).strip().casefold() for a in (config.get("agents") or ()) if str(a).strip()]
    if not allowed:
        return None
    subject = str(ctx.claims.get("sub") or "").strip().casefold()
    if subject in allowed:
        return None
    return ReasonCode.AGENT_NOT_ALLOWED, {"agent": ctx.claims.get("sub") or ""}


def blocked_categories(config, ctx: RuleContext) -> Breach:
    """Keep named categories out of agent purchases; everything else stays open."""
    blocked = {str(c).strip().casefold() for c in (config.get("categories") or ()) if str(c).strip()}
    if not blocked or ctx.category.casefold() not in blocked:
        return None
    return ReasonCode.STORE_CATEGORY_NOT_ALLOWED, {"category": ctx.category, "list": "blocked"}


def allowed_categories(config, ctx: RuleContext) -> Breach:
    """Sell only named categories to agents. The inverse of blocked_categories,
    kept as its own rule rather than folded into it: "allow only Dress" and
    "block Dress" are different sentences, and a store that had one must not be
    silently switched to the other."""
    allowed = {str(c).strip().casefold() for c in (config.get("categories") or ()) if str(c).strip()}
    if not allowed or ctx.category.casefold() in allowed:
        return None
    return ReasonCode.STORE_CATEGORY_NOT_ALLOWED, {"category": ctx.category, "list": "allowed"}


_PRESENCE: dict[str, Callable[[Any], bool]] = {
    "title": lambda p: bool((p.title or "").strip()),
    "price_paise": lambda p: p.price_paise is not None and p.price_paise > 0,
    "currency": lambda p: len(p.currency or "") == 3,
    "sku": lambda p: bool((p.sku or "").strip()),
    "image_url": lambda p: bool((p.image_url or "").strip()),
    "in_stock": lambda p: p.in_stock is not None,
}

PRODUCT_DATA_FIELDS = tuple(_PRESENCE)
DEFAULT_REQUIRED_FIELDS = ("title", "price_paise", "currency", "sku", "image_url", "in_stock")


def complete_product_data(config, ctx: RuleContext) -> Breach:
    """Only sell rows that publish everything an agent needs to describe them.

    An empty list means "require nothing", and is not the same as an absent key.
    `or DEFAULT_REQUIRED_FIELDS` conflated the two, because `[] or X` is `X`: a
    merchant who cleared every field in the form got the full default set back
    and then refusals they could not explain from a rule whose form showed
    nothing selected. Only a missing key -- a row that predates this config, or
    one written before the field existed -- falls back to the defaults.
    """
    required = config.get("required_fields")
    if required is None:
        required = DEFAULT_REQUIRED_FIELDS
    wanted = [f for f in required if f in _PRESENCE]
    missing = [f for f in wanted if not _PRESENCE[f](ctx.product)]
    if not missing:
        return None
    return ReasonCode.INCOMPLETE_PRODUCT_DATA, {"missing_fields": missing}


def per_order_ceiling(config, ctx: RuleContext) -> Breach:
    """Cap any single agent order, whatever the buyer's mandate allows."""
    limit = _int_or_none(config.get("amount_paise"))
    if limit is None or ctx.amount_paise <= limit:
        return None
    return ReasonCode.STORE_TXN_CAP_EXCEEDED, {
        "limit_paise": limit, "amount_paise": ctx.amount_paise,
    }


def daily_spend_cap(config, ctx: RuleContext) -> Breach:
    """Stop agent purchases once they total a limit in one calendar day.

    ctx.day_spent_paise counts orders already authorised today, so the amount
    being decided is added to it here -- the cap is on the day's total
    including this purchase, not on what was spent before it.
    """
    limit = _int_or_none(config.get("amount_paise"))
    if limit is None:
        return None
    projected = ctx.day_spent_paise + ctx.amount_paise
    if projected <= limit:
        return None
    return ReasonCode.STORE_DAILY_CAP_EXCEEDED, {
        "limit_paise": limit, "spent_today_paise": ctx.day_spent_paise,
        "projected_paise": projected, "amount_paise": ctx.amount_paise,
    }


def min_catalog_confidence(config, ctx: RuleContext) -> Breach:
    """Rows we extracted with low confidence need a human before they sell."""
    floor = config.get("min_confidence")
    if floor in (None, ""):
        return None
    floor = float(floor)
    if ctx.product.confidence >= floor:
        return None
    return ReasonCode.LOW_CONFIDENCE_ROW, {
        "confidence": ctx.product.confidence, "min_confidence": floor,
    }


MAX_TOLERANCE_BPS = 5_000


def price_change_tolerance(config, ctx: RuleContext) -> Breach:
    """Whether a price rise since the quote may be put to a human.

    Not reached through a phase, and it cannot make a purchase ALLOWED. The
    engine's exact-match price check is a platform invariant that runs with no
    active policy and with this rule in draft; all this rule can do is soften
    that refusal into an approval request when the rise is inside the band.

    Widening ALLOW instead would mean a buyer paying a price nobody quoted
    them: the Payment Link is created from the live price, and the webhook
    debits the mandate by whatever was actually paid. The quote is the only
    record of what the buyer consented to.
    """
    quoted, live = ctx.quoted_price_paise, ctx.live_price_paise
    if quoted <= 0 or live <= quoted:
        return None
    if not ctx.quote_from_caller:
        return None
    band = min(int(_int_or_none(config.get("tolerance_bps")) or 0), MAX_TOLERANCE_BPS)
    drift = (live - quoted) * 10_000 // quoted
    if drift > band:
        return None
    return ReasonCode.PRICE_CHANGED, {
        "quoted_paise": quoted, "live_paise": live,
        "drift_bps": drift, "tolerance_bps": band,
    }



_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec(
        kind=RuleKind.AGENT_ALLOWLIST, phase=Phase.AGENT, order=10,
        reason_code=ReasonCode.AGENT_NOT_ALLOWED,
        default_on_breach=OnBreach.REFUSE, default_config={"agents": []},
        check=agent_allowlist, label="Agent allowlist",
        description="Accept purchases only from named agents. While this is off, any "
                    "agent with a valid mandate may buy.",
    ),
    RuleSpec(
        kind=RuleKind.BLOCKED_CATEGORIES, phase=Phase.ITEM, order=10,
        reason_code=ReasonCode.STORE_CATEGORY_NOT_ALLOWED,
        default_on_breach=OnBreach.REFUSE, default_config={"categories": []},
        check=blocked_categories, label="Blocked categories",
        description="Keep named categories out of agent purchases entirely. Everything "
                    "else in the catalogue stays open.",
    ),
    RuleSpec(
        kind=RuleKind.ALLOWED_CATEGORIES, phase=Phase.ITEM, order=20,
        reason_code=ReasonCode.STORE_CATEGORY_NOT_ALLOWED,
        default_on_breach=OnBreach.REFUSE, default_config={"categories": []},
        check=allowed_categories, label="Allowed categories",
        description="Sell only the named categories to agents. Anything not on the list "
                    "is refused, including categories added to the catalogue later.",
    ),
    RuleSpec(
        kind=RuleKind.COMPLETE_PRODUCT_DATA, phase=Phase.ITEM, order=30,
        reason_code=ReasonCode.INCOMPLETE_PRODUCT_DATA,
        default_on_breach=OnBreach.REFUSE,
        default_config={"required_fields": list(DEFAULT_REQUIRED_FIELDS)},
        check=complete_product_data, label="Complete product data",
        description="Only sell products that publish every field an agent needs: name, "
                    "price, currency, SKU, image, stock.",
    ),
    RuleSpec(
        kind=RuleKind.PER_ORDER_CEILING, phase=Phase.MONEY, order=10,
        reason_code=ReasonCode.STORE_TXN_CAP_EXCEEDED,
        default_on_breach=OnBreach.ASK_MERCHANT, default_config={"amount_paise": None},
        check=per_order_ceiling, label="Per-order ceiling",
        description="Refuse any single agent order above this amount, whatever the "
                    "buyer's mandate allows.",
    ),
    RuleSpec(
        kind=RuleKind.DAILY_SPEND_CAP, phase=Phase.MONEY, order=20,
        reason_code=ReasonCode.STORE_DAILY_CAP_EXCEEDED,
        default_on_breach=OnBreach.REFUSE, default_config={"amount_paise": None},
        check=daily_spend_cap, label="Daily agent spend cap",
        description="Stop agent purchases once they total this much in a calendar day. "
                    "Storefront sales are unaffected.",
    ),
    RuleSpec(
        kind=RuleKind.MIN_CATALOG_CONFIDENCE, phase=Phase.TRUST, order=10,
        reason_code=ReasonCode.LOW_CONFIDENCE_ROW,
        default_on_breach=OnBreach.ASK_MERCHANT, default_config={"min_confidence": 0.0},
        check=min_catalog_confidence, label="Minimum catalogue confidence",
        description="Catalogue rows we extracted with less confidence than this need "
                    "your approval before they sell.",
    ),
    RuleSpec(
        kind=RuleKind.PRICE_CHANGE_TOLERANCE, phase=Phase.MONEY, order=99,
        reason_code=ReasonCode.PRICE_CHANGED,
        default_on_breach=OnBreach.ASK_BUYER, default_config={"tolerance_bps": 0},
        check=price_change_tolerance, label="Price change tolerance",
        description="If the listed price rose by no more than this between the agent's "
                    "quote and the capture attempt, ask a human instead of refusing. "
                    "A bigger move is always refused.",
    ),
)

REGISTRY: dict[str, RuleSpec] = {spec.kind: spec for spec in _SPECS}

_PHASED: tuple[RuleSpec, ...] = tuple(
    sorted((s for s in _SPECS if s.kind != RuleKind.PRICE_CHANGE_TOLERANCE),
           key=lambda s: (s.phase, s.order))
)


def in_phase(phase: Phase) -> Sequence[RuleSpec]:
    """The rules of one phase, in the order they are always evaluated."""
    return tuple(s for s in _PHASED if s.phase == phase)


def all_specs() -> Sequence[RuleSpec]:
    """Every rule, in the order the merchant's screen lists them."""
    return _PHASED + (REGISTRY[RuleKind.PRICE_CHANGE_TOLERANCE],)


def run(spec: RuleSpec, config: Mapping[str, Any], ctx: RuleContext) -> Breach:
    """Evaluate one rule, absorbing anything its config throws.

    Merchant config is arbitrary JSON that reached the database through a form,
    and this runs in the money path. A TypeError from a string where a number
    belonged must not become a 500 on a checkout.

    It fails OPEN -- the purchase is judged as though the rule were off. That
    is only defensible because the checks a store cannot be trusted to get
    right are not rules: the kill switch, the mandate's signed ceilings, and
    the exact-match price check all sit outside this system and run regardless.
    A misconfigured rule loosens the merchant's own limit and is reported to
    them; failing closed would take the store offline over a typo.
    """
    try:
        return spec.check(config or {}, ctx)
    except Exception:
        log.exception("policy rule %s could not be evaluated; treating it as off", spec.kind)
        return None
