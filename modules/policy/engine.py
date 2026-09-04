"""The gate. Deterministic, no model in the money path.

evaluate() is deterministic in its inputs and performs exactly ONE query: the
mandate row named by the verified jti. Everything else it needs about the store
-- the active policy, its rules, the day's authorised spend -- is resolved by
the caller and handed in as StoreFacts.

It is not "pure", and that word was doing damage here: the docstring claimed no
I/O while two queries had already crept in behind it. The property that
actually matters is weaker and checkable -- the same inputs give the same
verdict, and no rule function touches the ORM.

Two layers of authority are checked, and the lower ceiling wins:

  buyer mandate  -- what THIS agent may spend, signed, held by the agent
  store policy   -- what ANY agent may do here, set by the merchant

Check order is deliberate. Authenticity comes first: a caller holding a forged
mandate must not learn whether the store accepts agent traffic, what a product
costs, or where the caps sit.

The merchant's rules are configurable and toggleable, and the things that are
NOT rules are the load-bearing part of that design:

  the kill switch          on Merchant, applies with no active policy
  the mandate's ceilings   read from signed claims, no configurable consequence
  catalogue membership     we do not sell what we do not list
  stock and exact price    the live page, re-read at commit time

A merchant can turn every rule off, or have no active policy at all, and each
of those still holds. That is what makes it safe for a rule whose config
cannot be evaluated to fail open.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

from modules.ledger.models import LedgerEntry
from modules.policy import jws, registry
from modules.policy.choices import OnBreach, RuleKind, RuleState
from modules.policy.models import Mandate
from modules.policy.reasons import ReasonCode, StepUpActor, Verdict
from modules.policy.registry import Phase, RuleContext

ACTION = "checkout"


@dataclass(frozen=True)
class CheckoutRequest:
    tenant: object
    mandate_token: str
    product_url: str
    quoted_price_paise: int
    quantity: int = 1
    currency: str = "INR"
    quote_from_caller: bool = True


@dataclass(frozen=True)
class LiveFacts:
    """Re-read from the merchant's product page at commit time, by the caller."""
    price_paise: int
    in_stock: bool
    fetched_at: datetime


@dataclass(frozen=True)
class RuleView:
    """A PolicyRule flattened to what a decision needs. No model handle, so a
    checker cannot walk a relation into a query mid-decision."""
    kind: str
    config: Mapping
    on_breach: str


@dataclass(frozen=True)
class StoreFacts:
    """The store side of a decision, resolved before evaluate() is called.

    `rules` holds ONLY the rules that are enforcing -- draft and off ones are
    dropped by facts_for, so the engine never has to ask about state. An empty
    mapping is therefore both "no active policy" and "an active policy with
    everything switched off", which is correct: neither applies any rule.
    """
    agent_traffic_enabled: bool
    currency: str
    policy_uuid: str | None
    rules: Mapping[str, RuleView]
    day_spent_paise: int

    @classmethod
    def none(cls, *, agent_traffic_enabled=True, currency="INR") -> "StoreFacts":
        return cls(agent_traffic_enabled, currency, None, {}, 0)

    def rule(self, kind) -> RuleView | None:
        return self.rules.get(kind)


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason_code: str
    step_up_actor: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW


@dataclass(frozen=True)
class _Pending:
    """A rule that was breached, before we know which breach gets reported."""
    kind: str
    phase: Phase
    order: int
    reason_code: str
    verdict: str
    actor: str | None
    detail: dict


def _deny(reason, **detail) -> Decision:
    return Decision(Verdict.DENY, reason, None, detail)


def _step_up(reason, actor, **detail) -> Decision:
    return Decision(Verdict.STEP_UP, reason, actor, detail)


def _epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        return None


_VERDICT_FOR = {
    OnBreach.REFUSE: (Verdict.DENY, None),
    OnBreach.ASK_BUYER: (Verdict.STEP_UP, StepUpActor.BUYER),
    OnBreach.ASK_MERCHANT: (Verdict.STEP_UP, StepUpActor.MERCHANT),
}

_MONEY_CAPS = frozenset({
    ReasonCode.BUDGET_EXCEEDED,
    ReasonCode.STORE_TXN_CAP_EXCEEDED,
    ReasonCode.STORE_DAILY_CAP_EXCEEDED,
})


def _collect(phase: Phase, facts: StoreFacts, ctx: RuleContext,
             approved_reason: str | None) -> list[_Pending]:
    """Run one phase's rules. Iterates the REGISTRY, never the merchant's rows.

    That direction matters three ways: order is fixed regardless of what the
    merchant enabled or in what sequence, a kind with no row simply does not
    run (so a registry addition needs no backfill), and a row whose kind was
    retired is never reached.
    """
    pending = []
    for spec in registry.in_phase(phase):
        rule = facts.rule(spec.kind)
        if rule is None:
            continue
        breach = registry.run(spec, rule.config, ctx)
        if breach is None:
            continue
        reason_code, detail = breach
        if reason_code == approved_reason:
            continue
        verdict, actor = _VERDICT_FOR[rule.on_breach]
        pending.append(_Pending(spec.kind, spec.phase, spec.order,
                                reason_code, verdict, actor, detail))
    return pending


def _resolve(pending: list[_Pending]) -> _Pending:
    """Which of several simultaneous breaches gets reported.

    The strictest outcome wins: a refusal beats a request for approval, because
    reporting the approval would offer a human a purchase that another rule
    refuses outright.

    Among equals, if every breach is a money ceiling then the tighter limit
    wins -- that is the original "lower ceiling wins" rule, kept, and it is
    still what decides whether a store cap or a mandate cap gets reported.
    Across different kinds there is no common scale, so registry order decides,
    which at least gives the UI a sentence it can print: the rule listed first
    in Store policies is the one that answers.
    """
    hard = [p for p in pending if p.verdict == Verdict.DENY]
    if hard:
        return min(hard, key=lambda p: (p.phase, p.order))
    if all(p.reason_code in _MONEY_CAPS for p in pending):
        return min(pending, key=lambda p: p.detail["limit_paise"])
    return min(pending, key=lambda p: (p.phase, p.order))


def _decide(pending: list[_Pending], common: dict) -> Decision:
    """Turn the breaches into one decision, recording all of them.

    Every breached kind is written into detail, not just the winner. Without
    that, a rule which always loses the tiebreak would report zero refusals
    forever, and the merchant's screen would show it as never having fired.
    """
    winner = _resolve(pending)
    detail = {
        **common,
        **winner.detail,
        "deciding_kind": winner.kind,
        "breached_kinds": [p.kind for p in pending],
        "breached": [{"kind": p.kind, "reason": p.reason_code, "verdict": p.verdict,
                      "detail": p.detail} for p in pending],
    }
    return Decision(winner.verdict, winner.reason_code, winner.actor, detail)


def evaluate(request: CheckoutRequest, product, live: LiveFacts,
             facts: StoreFacts | None = None, now=None,
             approved_reason: str | None = None) -> Decision:
    """Decide. One query, and no rule function touches the ORM.

    `facts` is the store side, resolved by the caller via facts_for() below.
    Omitting it means "no active policy and default store settings", which is
    the right default for a unit test about mandate behaviour and wrong for
    the money path -- quote_and_gate always passes it.

    `approved_reason` carries a human's approval of ONE soft refusal -- the
    reason a step-up was raised for. That single check is skipped; every other
    check still runs, because approval happens minutes after the request and
    the page is re-read in between. A price that moved or stock that vanished
    must still stop the purchase. Hard denials are never skippable.
    """
    now = now or timezone.now()
    facts = facts if facts is not None else StoreFacts.none()

    try:
        claims = jws.verify(request.mandate_token)
    except jws.MandateInvalid as exc:
        return _deny(exc.reason, error=exc.detail)

    jti = claims.get("jti")
    mandate = Mandate.objects.filter(jti=jti).first() if jti else None
    if mandate is None:
        return _deny(ReasonCode.MANDATE_NOT_FOUND, jti=jti)
    if mandate.tenant_id != request.tenant.id:
        return _deny(ReasonCode.MANDATE_TENANT_MISMATCH, jti=str(jti))

    not_before, expires_at = _epoch(claims.get("nbf")), _epoch(claims.get("exp"))
    if expires_at is None or not_before is None:
        return _deny(ReasonCode.MANDATE_MALFORMED, error="missing nbf/exp")
    if now < not_before:
        return _deny(ReasonCode.MANDATE_NOT_YET_VALID, not_before=not_before.isoformat())
    if now >= expires_at:
        return _deny(ReasonCode.MANDATE_EXPIRED, expired_at=expires_at.isoformat())
    if mandate.revoked_at is not None:
        return _deny(ReasonCode.MANDATE_REVOKED, revoked_at=mandate.revoked_at.isoformat())

    if not facts.agent_traffic_enabled:
        return _deny(ReasonCode.STORE_AGENTS_DISABLED)

    currencies = {request.currency, claims.get("cur", "INR"), facts.currency}
    if len(currencies) != 1:
        return _deny(ReasonCode.CURRENCY_MISMATCH, seen=sorted(currencies))

    ctx = RuleContext(
        claims=claims, product=product,
        category=((product.raw or {}).get("category", "") if product else ""),
        live_price_paise=live.price_paise,
        quoted_price_paise=request.quoted_price_paise,
        quote_from_caller=request.quote_from_caller,
        amount_paise=None,
        day_spent_paise=facts.day_spent_paise,
        now=now,
    )

    breaches = _collect(Phase.AGENT, facts, ctx, approved_reason)
    if breaches:
        return _decide(breaches, {"url": request.product_url})

    if product is None:
        return _deny(ReasonCode.ITEM_NOT_IN_CATALOG, url=request.product_url)

    breaches = _collect(Phase.ITEM, facts, ctx, approved_reason)
    if breaches:
        return _decide(breaches, {"url": request.product_url, "category": ctx.category})
    mandate_categories = claims.get("cats") or []
    if mandate_categories and ctx.category not in mandate_categories:
        return _deny(ReasonCode.MANDATE_CATEGORY_NOT_ALLOWED, category=ctx.category)

    if not live.in_stock:
        return _deny(ReasonCode.OUT_OF_STOCK, url=request.product_url)
    if live.price_paise != request.quoted_price_paise:
        in_band = _price_drift_in_band(facts, ctx)
        if in_band is None:
            return _deny(ReasonCode.PRICE_CHANGED,
                         quoted_paise=request.quoted_price_paise,
                         live_paise=live.price_paise,
                         crawled_paise=product.price_paise)
        if approved_reason != ReasonCode.PRICE_CHANGED:
            actor, detail = in_band
            return _step_up(ReasonCode.PRICE_CHANGED, actor,
                            crawled_paise=product.price_paise,
                            policy_uuid=facts.policy_uuid,
                            deciding_kind=RuleKind.PRICE_CHANGE_TOLERANCE,
                            breached_kinds=[RuleKind.PRICE_CHANGE_TOLERANCE], **detail)

    amount = live.price_paise * request.quantity
    per_txn_cap = int(claims.get("cap", 0))
    total_cap = int(claims.get("total", 0))
    ceiling = int(claims.get("ceiling", 0))

    common = {
        "amount_paise": amount,
        "mandate_jti": str(jti),
        "mandate_cap_paise": per_txn_cap,
        "live_price_paise": live.price_paise,
        "url": request.product_url,
        "policy_uuid": facts.policy_uuid,
    }

    if mandate.spent_paise + amount > total_cap:
        return _deny(ReasonCode.MANDATE_TOTAL_EXCEEDED,
                     spent_paise=mandate.spent_paise, total_cap_paise=total_cap, **common)
    if ceiling and amount > ceiling:
        return _deny(ReasonCode.STEP_UP_CEILING_EXCEEDED, ceiling_paise=ceiling, **common)

    money_ctx = replace_amount(ctx, amount)
    breaches = _collect(Phase.MONEY, facts, money_ctx, approved_reason)
    if per_txn_cap and amount > per_txn_cap and approved_reason != ReasonCode.BUDGET_EXCEEDED:
        breaches.append(_Pending(
            "mandate_per_txn_cap", Phase.MONEY, 0, ReasonCode.BUDGET_EXCEEDED,
            Verdict.STEP_UP, StepUpActor.BUYER, {"limit_paise": per_txn_cap},
        ))
    if breaches:
        return _decide(breaches, common)

    breaches = _collect(Phase.TRUST, facts, money_ctx, approved_reason)
    if breaches:
        return _decide(breaches, common)

    return Decision(Verdict.ALLOW, ReasonCode.ALLOWED, None, common)


def replace_amount(ctx: RuleContext, amount: int) -> RuleContext:
    """RuleContext with the order total filled in, for the phases that have one."""
    return RuleContext(
        claims=ctx.claims, product=ctx.product, category=ctx.category,
        live_price_paise=ctx.live_price_paise, quoted_price_paise=ctx.quoted_price_paise,
        quote_from_caller=ctx.quote_from_caller, amount_paise=amount,
        day_spent_paise=ctx.day_spent_paise, now=ctx.now,
    )


def _price_drift_in_band(facts: StoreFacts, ctx: RuleContext) -> tuple[str, dict] | None:
    """Is this price rise one the merchant is willing to put to a human?

    Returns (actor, detail) when it is, None when the refusal stands. None is
    the answer in every case that matters: no active policy, the rule absent or
    not enforcing, on_breach=refuse, tolerance zero, a price that FELL rather
    than rose, the band exceeded, or a quote the caller defaulted rather than
    the agent naming it.

    Deliberately not consulted about approval. An approved PRICE_CHANGED
    step-up still has to come back through here, so a price that moved again
    -- past the band -- between the approval request and the approval is
    refused rather than waved through on the strength of the old consent.
    """
    rule = facts.rule(RuleKind.PRICE_CHANGE_TOLERANCE)
    if rule is None:
        return None
    spec = registry.REGISTRY[RuleKind.PRICE_CHANGE_TOLERANCE]
    breach = registry.run(spec, rule.config, ctx)
    if breach is None:
        return None
    verdict, actor = _VERDICT_FOR[rule.on_breach]
    if verdict != Verdict.STEP_UP:
        return None
    return actor, breach[1]


def _caller_identity(request: CheckoutRequest) -> dict:
    """Who the chain records as the caller, resolved only from a mandate that
    actually verified.

    An unverified token contributes nothing: a caller whose signature failed
    must not be written into the audit chain as though it were a known agent,
    and the audit screen shows those rows as an unsigned caller. Copied onto
    the entry rather than joined at read time, because the entry is permanent
    and a mandate is not -- one revoked or reissued next month must not change
    what a decision made today says about itself.
    """
    try:
        claims = jws.verify(request.mandate_token)
    except jws.MandateInvalid:
        return {}
    mandate = Mandate.objects.filter(
        jti=claims.get("jti"), tenant_id=request.tenant.id,
    ).select_related("buyer").first()
    if mandate is None:
        return {}
    identity = {"agent": mandate.subject}
    if mandate.buyer is not None:
        identity["buyer_uuid"] = str(mandate.buyer.uuid)
        identity["buyer_label"] = mandate.buyer.display_name
    return identity


def evaluate_and_record(request: CheckoutRequest, product, live: LiveFacts,
                        facts: StoreFacts | None = None, now=None,
                        approved_reason: str | None = None,
                        action: str = ACTION) -> tuple[Decision, LedgerEntry]:
    """Decide, then append to the hash chain. Every outcome is recorded --
    allowances and refusals alike."""
    decision = evaluate(request, product, live, facts, now=now,
                        approved_reason=approved_reason)
    detail = dict(decision.detail)
    detail.update(_caller_identity(request))
    if decision.step_up_actor:
        detail["step_up_actor"] = decision.step_up_actor
    if approved_reason:
        detail["approved_reason"] = approved_reason
    entry = LedgerEntry.append(
        tenant=request.tenant,
        action=action,
        decision=decision.verdict,
        reason_code=decision.reason_code,
        detail=detail,
    )
    return decision, entry


def facts_for(tenant, *, now=None) -> StoreFacts:
    """Resolve the store side of a decision. The only DB access in this file
    besides the mandate lookup, and it happens before evaluate() is entered.

    Lives here rather than in agentmcp.checkout so the step-up approval path
    and the checkout path cannot drift apart on how a policy is loaded.
    """
    from modules.policy import services

    policy = services.active_for(tenant)
    if policy is None:
        return StoreFacts.none(agent_traffic_enabled=tenant.agent_traffic_enabled,
                               currency=tenant.currency)

    rules = {
        r.kind: RuleView(r.kind, r.config or {}, r.on_breach)
        for r in policy.rules.filter(state=RuleState.ENFORCING)
        if r.kind in registry.REGISTRY
    }
    spent = (services.day_spent_paise(tenant)
             if RuleKind.DAILY_SPEND_CAP in rules else 0)
    return StoreFacts(
        agent_traffic_enabled=tenant.agent_traffic_enabled,
        currency=tenant.currency,
        policy_uuid=str(policy.uuid),
        rules=rules,
        day_spent_paise=spent,
    )
