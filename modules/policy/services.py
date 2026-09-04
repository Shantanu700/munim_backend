"""Writes to policies and rules, and the audit trail of those writes.

Every mutation a merchant can perform goes through a function here rather than
through a serializer's save(), for two reasons.

The invariants are cross-row. "At most one active policy" and "soft-deleting a
policy also retires its rules" cannot be expressed by saving one instance, and
doing them in the wrong order leaves a window where the database has zero
active policies or a deleted policy still holding the active slot.

And the ledger append belongs at the call site, not in a signal. LedgerEntry
takes a row lock and advances a per-tenant sequence, so an append hidden inside
save() would renumber the money chain from any fixture that happened to create
a policy -- and make every test that asserts on `seq` depend on unrelated
setup.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.enums import Status
from modules.core.models import Merchant
from modules.ledger.models import LedgerEntry
from modules.policy import jws
from modules.policy.choices import RuleState
from modules.policy.models import Mandate, PolicyRule, StorePolicy
from modules.policy.registry import REGISTRY, all_specs
from modules.policy.reasons import Verdict
from modules.twin.orders import Order

POLICY_CHANGE = "policy_change"

MANDATE_CHANGE = "mandate_change"

CONFIG = "CONFIG"

ISSUER = "munim"
DEFAULT_TTL_HOURS = 48
MAX_TTL_HOURS = 24 * 90


def _live(queryset):
    return queryset.exclude(status=Status.DELETED)


def _record(merchant, by_user, reason_code, detail, *, action=POLICY_CHANGE):
    """Append one policy change to the tenant's chain.

    On the money chain deliberately: a merchant who tightened a rule after a
    refusal, or loosened one before an approval, should be answerable for that
    from the same tamper-evident record as the decisions themselves.
    """
    return LedgerEntry.append(
        tenant=merchant, action=action, decision=CONFIG,
        reason_code=reason_code,
        detail={
            **detail,
            "actor_user_id": getattr(by_user, "pk", None),
            "actor_email": getattr(by_user, "email", ""),
            "actor_name": getattr(by_user, "name", "") or getattr(by_user, "username", ""),
        },
    )



def active_for(merchant) -> StorePolicy | None:
    """The policy in force, or None. None means no store rules apply at all."""
    return _live(StorePolicy.objects.filter(merchant=merchant, is_active=True)).first()


def day_start(now=None):
    """Midnight of the merchant's day, not UTC's.

    TIME_ZONE is Asia/Kolkata and USE_TZ is on, so a 04:00 local order is
    22:30 UTC the previous day. Getting this wrong would move a daily cap's
    reset into the middle of the merchant's evening.
    """
    now = now or timezone.now()
    return timezone.make_aware(datetime.combine(timezone.localdate(now), time.min))


def day_spent_paise(merchant, since=None) -> int:
    """What agents have already committed this store to today.

    Counts PENDING as well as PAID. Payment is asynchronous, so nothing is PAID
    at the moment the gate has to decide -- counting only PAID would let an
    agent open any number of links at once and settle them later, and the cap
    would never bind. Counting PENDING needs the expiry reaper in
    modules/recovery/tasks.py to drain abandoned links, or the cap ratchets
    shut on the merchant.

    ponytail: advisory under concurrency. Simultaneous checkouts each read this
    same total and each pass. Reserve inside the Order create transaction if
    that ever matters more than the simplicity does.
    """
    return Order.objects.filter(
        tenant=merchant,
        authorized_at__gte=since or day_start(),
        status__in=(Order.Status.PENDING, Order.Status.PAID),
    ).aggregate(total=Coalesce(Sum("amount_paise"), 0))["total"]



def seed_rules(policy) -> int:
    """Give a policy one row per registry kind. Idempotent.

    Called on create, and again on read from the management API, so a kind
    added to the registry later shows up on policies that predate it without a
    backfill migration per release. Never called from the engine -- a decision
    path must not write.

    A soft-deleted rule reads as missing here -- PolicyRule.objects excludes
    it and the partial unique index does not count it -- so it comes back as a
    fresh draft rather than resurrecting the merchant's old config.

    Returns how many rows were actually added, which is why it re-counts
    instead of trusting bulk_create: under ignore_conflicts Postgres cannot
    report which rows it skipped, so bulk_create hands back everything it was
    given whether it inserted it or not.
    """
    before = set(PolicyRule.objects.filter(policy=policy).values_list("kind", flat=True))
    missing = [spec for spec in all_specs() if spec.kind not in before]
    if not missing:
        return 0
    PolicyRule.objects.bulk_create(
        [PolicyRule(policy=policy, kind=spec.kind, config=dict(spec.default_config),
                    state=RuleState.DRAFT, on_breach=spec.default_on_breach)
         for spec in missing],
        ignore_conflicts=True,
    )
    return PolicyRule.objects.filter(policy=policy).count() - len(before)


@transaction.atomic
def create_policy(merchant, *, name, by_user) -> StorePolicy:
    policy = StorePolicy.objects.create(merchant=merchant, name=name)
    seed_rules(policy)
    _record(merchant, by_user, "POLICY_CREATED",
            {"policy_uuid": str(policy.uuid), "name": name})
    return policy


@transaction.atomic
def activate(policy, *, by_user) -> StorePolicy:
    """Make this the store's one live policy.

    The Merchant row is locked first, and it is the same row LedgerEntry.append
    locks -- taken in the same order here, so activating and then appending
    cannot deadlock against a concurrent request doing the reverse.

    The naive "clear them all, then set this one" has a window with no active
    policy, and two concurrent activations both see a free slot and one of them
    dies on the unique index as a 500. The lock closes both.
    """
    Merchant.objects.select_for_update().get(pk=policy.merchant_id)
    previous = _live(StorePolicy.objects
                     .filter(merchant_id=policy.merchant_id, is_active=True)
                     .exclude(pk=policy.pk)).first()

    StorePolicy.objects.filter(merchant_id=policy.merchant_id, is_active=True) \
        .exclude(pk=policy.pk).update(is_active=False)
    StorePolicy.objects.filter(pk=policy.pk).update(is_active=True, status=Status.UPDATED)

    _record(policy.merchant, by_user, "POLICY_ACTIVATED", {
        "policy_uuid": str(policy.uuid), "name": policy.name,
        "previous_policy_uuid": str(previous.uuid) if previous else None,
        "previous_name": previous.name if previous else None,
    })
    policy.refresh_from_db(fields=["is_active", "status"])
    return policy


@transaction.atomic
def deactivate(policy, *, by_user) -> StorePolicy:
    """Leave the store with no policy in force, and so no store rules."""
    Merchant.objects.select_for_update().get(pk=policy.merchant_id)
    StorePolicy.objects.filter(pk=policy.pk).update(is_active=False, status=Status.UPDATED)
    _record(policy.merchant, by_user, "POLICY_DEACTIVATED",
            {"policy_uuid": str(policy.uuid), "name": policy.name})
    policy.refresh_from_db(fields=["is_active", "status"])
    return policy


@transaction.atomic
def soft_delete(policy, *, by_user) -> None:
    """Retire a policy and its rules.

    on_delete=CASCADE does nothing for a soft delete, so the rules are retired
    explicitly -- otherwise they stay live, invisible through `policy.rules`
    but still counted by any analytics query that filters on kind alone.

    is_active is cleared in the same statement as the delete. The active-slot
    index ignores deleted rows, so a deleted row that kept is_active=True would
    free the slot while still reading as active, and code that trusts the
    column alone would see two active policies.
    """
    Merchant.objects.select_for_update().get(pk=policy.merchant_id)
    PolicyRule.objects.filter(policy=policy).update(status=Status.DELETED)
    StorePolicy.objects.filter(pk=policy.pk).update(status=Status.DELETED, is_active=False)
    _record(policy.merchant, by_user, "POLICY_DELETED",
            {"policy_uuid": str(policy.uuid), "name": policy.name})


@transaction.atomic
def rename(policy, *, name, by_user) -> StorePolicy:
    was = policy.name
    StorePolicy.objects.filter(pk=policy.pk).update(name=name, status=Status.UPDATED)
    _record(policy.merchant, by_user, "POLICY_RENAMED",
            {"policy_uuid": str(policy.uuid), "from": was, "to": name})
    policy.refresh_from_db(fields=["name", "status"])
    return policy


@transaction.atomic
def update_rule(rule, *, by_user, config=None, state=None, on_breach=None) -> PolicyRule:
    """Change one rule, and record what changed.

    One ledger entry per call carrying every field that moved, not one per
    field -- a merchant adjusting a cap and switching it on in the same save
    made one change, and three chain entries would make the history unreadable.
    """
    changes = []
    fields = ["status"]

    if config is not None and config != rule.config:
        changes.append({"field": "config", "from": rule.config, "to": config})
        rule.config = config
        fields.append("config")

    if on_breach is not None and on_breach != rule.on_breach:
        changes.append({"field": "on_breach", "from": rule.on_breach, "to": on_breach})
        rule.on_breach = on_breach
        fields.append("on_breach")

    if state is not None and state != rule.state:
        changes.append({"field": "state", "from": rule.state, "to": state})
        rule.state = state
        fields.append("state")
        if state == RuleState.ENFORCING and rule.first_enforced_at is None:
            rule.first_enforced_at = timezone.now()
            fields.append("first_enforced_at")

    if not changes:
        return rule

    rule.status = Status.UPDATED
    PolicyRule.objects.filter(pk=rule.pk).update(
        **{f: getattr(rule, f) for f in fields}
    )
    _record(rule.policy.merchant, by_user, "POLICY_RULE_UPDATED", {
        "policy_uuid": str(rule.policy.uuid), "policy_name": rule.policy.name,
        "rule_kind": rule.kind, "rule_label": REGISTRY[rule.kind].label,
        "changes": changes,
    })
    return rule


@transaction.atomic
def set_kill_switch(merchant, *, enabled: bool, by_user) -> Merchant:
    """The store-wide switch. Not a rule, and reachable with no active policy."""
    if merchant.agent_traffic_enabled == enabled:
        return merchant
    Merchant.objects.filter(pk=merchant.pk).update(agent_traffic_enabled=enabled)
    merchant.agent_traffic_enabled = enabled
    _record(merchant, by_user,
            "AGENT_TRAFFIC_ENABLED" if enabled else "AGENT_TRAFFIC_DISABLED",
            {"agent_traffic_enabled": enabled})
    return merchant


def history(merchant):
    """Every policy change, newest first. Reads the money chain, filtered."""
    return LedgerEntry.objects.filter(
        Q(tenant=merchant) & Q(action=POLICY_CHANGE)
    ).order_by("-seq")



def rupees_to_paise(value) -> int:
    """Money, in Decimal, never in float. The canonical converter.

    Half-up rather than int()'s truncation, which is what the management command
    did: `--cap 50.999` silently became Rs 50.99 of authority, and a cap quietly
    lower than the buyer typed is a cap that refuses a purchase they authorised.
    The API's DecimalField refuses a third decimal place at the door; this is
    what the CLI gets.
    """
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"),
                                                        rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{value!r} is not an amount") from exc


@transaction.atomic
def issue_mandate(merchant, *, subject, per_txn_cap_paise, total_cap_paise,
                  step_up_ceiling_paise, allowed_categories=None,
                  ttl_hours=DEFAULT_TTL_HOURS, not_before=None,
                  buyer=None, by_user=None, via="api", kid=None, now=None) -> Mandate:
    """Sign one mandate, store its row, and record the issue on the money chain.

    The ONLY place a mandate is minted -- the management command is a thin
    wrapper over this. That matters more here than for the other services in this
    file: the engine reads the CAPS from the verified claims and the mandate's
    identity from `jti`, so a second builder that shaped the claim set
    differently would not fail, it would produce mandates the gate answers
    MANDATE_NOT_FOUND for or, worse, honours with caps that disagree with the row
    the buyer can see.

    Atomic, which the command was not. Signing before the insert with both inside
    one transaction is what stops the two ways this can half-happen: a token
    handed to an agent with no row behind it, which the gate then refuses
    forever, and a row with no ledger entry, which makes the issue invisible in
    the audit trail. jws.sign is pure CPU, so holding the transaction across it
    costs nothing -- unlike the Razorpay call in core/services.verify_and_record,
    which is deliberately outside one.

    Takes no lock of its own. There is no cross-row invariant to protect -- unlike
    activate(), which keeps an active slot unique -- and the only lock in the call
    is the one LedgerEntry.append takes on the Merchant row, last.

    `by_user` is the login answerable for this, or None for a CLI run; `via`
    distinguishes those two, so a blank actor on the audit screen reads as
    "issued from the command line" rather than as missing data.
    """
    cap, total = int(per_txn_cap_paise), int(total_cap_paise)
    ceiling, ttl = int(step_up_ceiling_paise), int(ttl_hours)
    categories = list(allowed_categories or [])

    if min(cap, total, ceiling) <= 0:
        raise ValueError("Caps must be positive amounts.")
    if cap > total:
        raise ValueError("The per-transaction cap cannot exceed the total budget.")
    if ceiling < cap:
        raise ValueError("The step-up ceiling cannot be below the per-transaction "
                         "cap; nothing would ever pass.")
    if not 1 <= ttl <= MAX_TTL_HOURS:
        raise ValueError(f"The lifetime must be between 1 and {MAX_TTL_HOURS} hours.")
    if not subject:
        raise ValueError("A mandate has to name the agent it authorises.")
    if not merchant.domain:
        raise ValueError("This store has no domain yet, so nothing can be signed "
                         "for it. Finish the catalogue step first.")

    now = now or timezone.now()
    nbf = not_before or now
    expires = nbf + timedelta(hours=ttl)
    jti = uuid.uuid4()
    kid = kid or jws.active_kid()
    currency = merchant.currency or "INR"

    claims = {
        "jti": str(jti),
        "iss": ISSUER,
        "sub": subject,
        "aud": merchant.domain,
        "iat": int(now.timestamp()),
        "nbf": int(nbf.timestamp()),
        "exp": int(expires.timestamp()),
        "cap": cap,
        "total": total,
        "ceiling": ceiling,
        "cur": currency,
        "cats": categories,
    }
    token = jws.sign(claims, kid)

    mandate = Mandate.objects.create(
        tenant=merchant, jti=jti, kid=kid, subject=subject, buyer=buyer,
        per_txn_cap_paise=cap, total_cap_paise=total, step_up_ceiling_paise=ceiling,
        currency=currency, allowed_categories=categories,
        not_before=nbf, expires_at=expires, token=token,
    )

    _record(merchant, by_user, "MANDATE_ISSUED", {
        "mandate_uuid": str(mandate.uuid),
        "mandate_jti": str(jti),
        "kid": kid,
        "agent": subject,
        "via": via,
        "cap_paise": cap,
        "total_paise": total,
        "ceiling_paise": ceiling,
        "currency": currency,
        "categories": categories,
        "not_before": nbf.isoformat(),
        "expires_at": expires.isoformat(),
        "buyer_uuid": str(buyer.uuid) if buyer else None,
        "buyer_label": buyer.display_name if buyer else "",
    }, action=MANDATE_CHANGE)
    return mandate


@transaction.atomic
def revoke_mandate(mandate, *, by_user=None, now=None) -> Mandate:
    """Kill a mandate. Idempotent: revoking a revoked one changes nothing.

    Revocation is the one thing a signature cannot carry -- see Mandate's own
    docstring -- so this column is the only place it can live, and
    engine.evaluate reads exactly it. Which also means: never soft-delete a
    Mandate to retire it. Mandate.objects is a DeleteStatusManager, so
    status=DELETED makes the row invisible to the engine's lookup and the gate
    answers MANDATE_NOT_FOUND instead of MANDATE_REVOKED -- the same refusal with
    a reason code that lies about why.

    Allowed on an expired mandate, on purpose. A buyer pressing Revoke next to a
    lapsed row is asking for it to be unusable, and answering 409 would be
    pedantry; `was_live` in the entry records which of the two it actually was.

    Locks the Merchant row first and the mandate second, the same order every
    other writer in this file uses and the same row LedgerEntry.append will
    re-take, so a revoke and a concurrent append cannot deadlock against each
    other.
    """
    now = now or timezone.now()
    Merchant.objects.select_for_update().get(pk=mandate.tenant_id)
    locked = Mandate.objects.select_for_update().filter(pk=mandate.pk).first()
    if locked is None:
        return mandate
    if locked.revoked_at is not None:
        return locked

    Mandate.objects.filter(pk=mandate.pk).update(revoked_at=now, status=Status.UPDATED)

    _record(locked.tenant, by_user, "MANDATE_REVOKED", {
        "mandate_uuid": str(locked.uuid),
        "mandate_jti": str(locked.jti),
        "agent": locked.subject,
        "spent_paise": locked.spent_paise,
        "unspent_paise": locked.remaining_paise,
        "was_live": locked.is_live(now),
        "expires_at": locked.expires_at.isoformat(),
        "revoked_at": now.isoformat(),
    }, action=MANDATE_CHANGE)

    mandate.revoked_at = now
    mandate.status = Status.UPDATED
    return mandate


__all__ = [
    "CONFIG", "MANDATE_CHANGE", "MAX_TTL_HOURS", "POLICY_CHANGE", "activate",
    "active_for", "create_policy", "day_spent_paise", "day_start", "deactivate",
    "history", "issue_mandate", "rename", "revoke_mandate", "rupees_to_paise",
    "seed_rules", "set_kill_switch", "soft_delete", "update_rule",
]
