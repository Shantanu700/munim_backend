"""What the policy screen reports: refusals held back, approvals, last change.

Everything here is derived from the ledger and from StepUp rows. Nothing is
counted into a column on the policy itself, because a counter is a second
source of truth about a refusal and the ledger already is the first one -- and
a counter cannot be recomputed after a bug, which is the whole reason the chain
exists.

The per-rule numbers read `detail.breached_kinds`, written by
engine._decide for every rule breached in a decision rather than only the one
that was reported. A rule that always loses the tiebreak would otherwise show
zero refusals for as long as it existed.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from modules.ledger.models import LedgerEntry
from modules.policy.choices import RuleKind, RuleState
from modules.policy.models import StepUp
from modules.policy.reasons import Verdict
from modules.policy.registry import (
    DEFAULT_REQUIRED_FIELDS,
    PRODUCT_DATA_FIELDS,
    _int_or_none,
)
from modules.policy.services import POLICY_CHANGE, active_for

WINDOW_DAYS = 30


def since(days=WINDOW_DAYS):
    return timezone.now() - timedelta(days=days)


def _decisions(merchant, days=WINDOW_DAYS):
    """Money decisions in the window. Excludes the policy-change entries that
    share the chain -- a merchant editing a rule is not a refusal."""
    return LedgerEntry.objects.filter(
        tenant=merchant, action="checkout", created_at__gte=since(days),
    ).exclude(action=POLICY_CHANGE)


def _held_back(entry) -> int:
    """What a refused decision would have charged.

    amount_paise is absent on refusals raised before the amount is known -- a
    blocked agent or a category block answers before the live price is
    accepted. Those are real refusals with no rupee figure attached, and
    reporting them as zero is honest; inventing the catalogue price would not
    be, since that is not what would have been charged.
    """
    return int(entry.detail.get("amount_paise") or 0)


def rule_counts(merchant, days=WINDOW_DAYS) -> dict[str, dict]:
    """Per rule kind: how often it refused, and how much it held back.

    A decision that breached three rules counts against all three. That is
    deliberate: the merchant's question is "is this rule doing anything", not
    "was this rule the one that got reported", and summing these will therefore
    exceed the store total. The tiles use the store total for that reason.
    """
    counts: dict[str, dict] = {}
    entries = _decisions(merchant, days).filter(
        decision__in=(Verdict.DENY, Verdict.STEP_UP),
    ).only("decision", "detail")
    for entry in entries:
        held = _held_back(entry)
        for kind in entry.detail.get("breached_kinds") or ():
            bucket = counts.setdefault(kind, {"refused": 0, "stepped_up": 0,
                                              "held_back_paise": 0})
            if entry.decision == Verdict.DENY:
                bucket["refused"] += 1
            else:
                bucket["stepped_up"] += 1
            bucket["held_back_paise"] += held
    return counts


def tiles(merchant, policy, days=WINDOW_DAYS) -> dict:
    """The four numbers across the top of the screen."""
    rules = list(policy.rules.all()) if policy is not None else []

    refusals = _decisions(merchant, days).filter(decision=Verdict.DENY) \
        .exclude(detail__breached_kinds__isnull=True).only("detail")
    refused, held_back = 0, 0
    for entry in refusals:
        if entry.detail.get("breached_kinds"):
            refused += 1
            held_back += _held_back(entry)

    step_ups = StepUp.objects.filter(tenant=merchant, created_at__gte=since(days))
    last = LedgerEntry.objects.filter(tenant=merchant, action=POLICY_CHANGE) \
        .order_by("-seq").first()

    return {
        "rules_enforcing": sum(r.state == RuleState.ENFORCING for r in rules),
        "rules_defined": len(rules),
        "refused_30d": refused,
        "held_back_paise_30d": held_back,
        "step_ups_30d": step_ups.count(),
        "step_ups_approved_30d": step_ups.filter(status=StepUp.Status.APPROVED).count(),
        "step_ups_expired_30d": step_ups.filter(
            status=StepUp.Status.PENDING, expires_at__lt=timezone.now()).count(),
        "last_change_at": last.created_at if last else None,
        "last_change_summary": _summarise(last) if last else "",
    }


def _summarise(entry) -> str:
    """The "last change" tile's caption, in the merchant's own terms."""
    detail = entry.detail or {}
    if entry.reason_code == "POLICY_RULE_UPDATED":
        fields = ", ".join(c["field"] for c in detail.get("changes") or [])
        return f"{detail.get('rule_label') or detail.get('rule_kind')}: {fields}"
    return {
        "POLICY_CREATED": f"Created {detail.get('name', '')}",
        "POLICY_ACTIVATED": f"Activated {detail.get('name', '')}",
        "POLICY_DEACTIVATED": f"Deactivated {detail.get('name', '')}",
        "POLICY_DELETED": f"Deleted {detail.get('name', '')}",
        "POLICY_RENAMED": f"Renamed to {detail.get('to', '')}",
        "AGENT_TRAFFIC_ENABLED": "Agent traffic switched on",
        "AGENT_TRAFFIC_DISABLED": "Agent traffic switched off",
    }.get(entry.reason_code, entry.reason_code)




def store_card(merchant, policy=None) -> dict:
    """The dashboard's store-policy panel: the three values a merchant reads.

    Every value carries `configured`, `enforcing` and `config_valid` beside it,
    and none of them is a bare number. A cap printed without `enforcing` shows a
    limit that refuses nothing when the rule is a draft; a cap printed without
    `config_valid` shows a limit the engine is currently failing OPEN on, which
    RuleSerializer.get_config_valid explains is otherwise invisible. The client
    renders "(off)" and a warning from these -- which is also how the design's
    "Dress (off)" is produced. That is a rule STATE, not a category name, so the
    two are returned separately and must never be collapsed into one list.

    Resolved through services.active_for, never StorePolicy.is_active alone: the
    partial unique index ignores soft-deleted rows, so a deleted policy frees
    the active slot while its own column still reads True (StorePolicy.is_live).
    Pass `policy` in when the caller already has it.

    A policy may legitimately have no row for a kind. seed_rules() is called on
    read from the MANAGEMENT api, not from here -- this is a read path and a read
    path must not write -- so a policy created before a kind existed and never
    opened in the editor simply has no row. Absent is reported as "not
    configured"; nothing here may KeyError on it.
    """
    if policy is None:
        policy = active_for(merchant)
    rules = {r.kind: r for r in policy.rules.all()} if policy is not None else {}

    return {
        "agent_traffic_enabled": merchant.agent_traffic_enabled,
        "currency": merchant.currency,
        "policy_name": policy.name if policy is not None else "",
        "has_active_policy": policy is not None,

        "per_order_ceiling": _cap(rules.get(RuleKind.PER_ORDER_CEILING)),
        "daily_spend_cap": _cap(rules.get(RuleKind.DAILY_SPEND_CAP)),
        "required_product_fields": _required(rules.get(RuleKind.COMPLETE_PRODUCT_DATA)),
        "allowed_categories": _categories(rules.get(RuleKind.ALLOWED_CATEGORIES)),
        "blocked_categories": _categories(rules.get(RuleKind.BLOCKED_CATEGORIES)),
    }


def _rule_meta(rule) -> dict:
    """Whether a rule exists, whether it bites, and whether it can be evaluated.

    config_valid mirrors RuleSerializer._validated rather than re-deriving it.
    That import pulls DRF into this module, which its siblings avoid; it is not
    a cycle (serializers imports models and registry, never analytics), and
    registry.py's DRF-free rule is about the MONEY path, which this is not on. A
    second config-validation table would be the worse trade.
    """
    from modules.policy.api.v1.serializers import CONFIG_SERIALIZERS

    if rule is None:
        return {"configured": False, "enforcing": False, "state": "",
                "config_valid": True, "config_errors": {}}
    body = CONFIG_SERIALIZERS[rule.kind](data=rule.config or {})
    ok = body.is_valid()
    return {
        "configured": True,
        "enforcing": rule.is_enforcing,
        "state": rule.state,
        "config_valid": ok,
        "config_errors": {} if ok else body.errors,
    }


def _cap(rule) -> dict:
    """A money ceiling. None means NO LIMIT and is emphatically not zero.

    Via registry._int_or_none rather than a second hand-rolled ""/None check: a
    cap of zero would refuse every purchase in the store, and that function is
    the definition of what a merchant's blank cap field means.

    A value int() cannot read is reported as unlimited, which is not a
    fallback -- it is what the store is actually doing. registry.run absorbs the
    checker's exception and judges the purchase as though the rule were off, so
    a cap of "lots" enforces nothing. Saying so, next to config_valid=False, is
    the only description of this row a merchant would not be misled by; raising
    here would instead take the whole dashboard down over one typo.
    """
    base = _rule_meta(rule)
    try:
        amount = _int_or_none((rule.config or {}).get("amount_paise")) if rule else None
    except (TypeError, ValueError):
        amount = None
    return {**base, "amount_paise": amount, "unlimited": amount is None}


def _required(rule) -> dict:
    """The details an item must publish before it can sell. Three states.

      - no row, or {"required_fields": null} -> the six defaults. The checker's
        test is `if required is None`, so an explicit JSON null behaves exactly
        like an absent key. Do NOT use `"required_fields" in config`.
      - {"required_fields": []}              -> require nothing. `[] or DEFAULT`
        is DEFAULT, which is how a merchant who cleared every box in the form
        got the full default set back and then refusals they could not explain.
      - a list -> that list, FILTERED to names the checker knows. The checker
        drops unknown names, so ["title", "colour"] requires ONE field and the
        count on the card is len(wanted) -- never len(config["required_fields"]).

    Mirrors registry.complete_product_data line for line. Any drift and the card
    reports a number the engine does not enforce.
    """
    base = _rule_meta(rule)
    required = (rule.config or {}).get("required_fields") if rule else None
    source = "default" if required is None else "configured"
    if required is None:
        required = DEFAULT_REQUIRED_FIELDS
    wanted = [f for f in required if f in PRODUCT_DATA_FIELDS]
    return {
        **base,
        "fields": wanted,
        "count": len(wanted),
        "source": source,
        "requires_nothing": source == "configured" and not wanted,
    }


def _categories(rule) -> dict:
    """A category list. Empty means the rule does nothing.

    Both checkers return None on an empty list, so an empty allowlist is
    UNRESTRICTED -- not "nothing sells" -- and an empty blocklist is inert.
    Same comprehension as the checkers (strip, drop blanks) but the merchant's
    own casing is kept: only the checkers casefold, and only to compare.
    """
    base = _rule_meta(rule)
    raw = (rule.config or {}).get("categories") or () if rule else ()
    names = [str(c).strip() for c in raw if str(c).strip()]
    return {**base, "categories": names, "unrestricted": not names}


def stopped_by(merchant, kind, days=WINDOW_DAYS) -> dict:
    """"What this policy stopped", for one rule.

    Zero refusals is an ambiguous number on its own -- it means either the rule
    works or nobody ever switched it on -- so the caller pairs this with the
    rule's own first_enforced_at to say which.
    """
    counts = rule_counts(merchant, days).get(kind) or {}
    return {
        "refused": counts.get("refused", 0),
        "stepped_up": counts.get("stepped_up", 0),
        "held_back_paise": counts.get("held_back_paise", 0),
    }
