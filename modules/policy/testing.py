"""Build a configured policy in one line.

Shipped rather than kept in tests/, so it cannot drift from the seeding the API
actually performs -- it calls the production services.seed_rules, which means a
kind added to the registry appears in every test that uses this without anyone
remembering to update a fixture.
"""
from __future__ import annotations

from modules.policy.choices import RuleState
from modules.policy.models import PolicyRule, StorePolicy
from modules.policy.registry import REGISTRY
from modules.policy.services import seed_rules


def policy_with(merchant, *, name="Test policy", active=True, **rules) -> StorePolicy:
    """A policy where exactly the named rules enforce and the rest are drafts.

        policy_with(tenant, per_order_ceiling={"amount_paise": 600_000})
        policy_with(tenant, blocked_categories={"categories": ["Co-Ord Sets"]})
        policy_with(tenant, per_order_ceiling={"amount_paise": 600_000,
                                               "on_breach": "refuse"})
        policy_with(tenant, name="Weekend", active=False, ...)

    Inside a rule's dict, `state` and `on_breach` are settings and everything
    else is that rule's config. Omitting `on_breach` takes the registry
    default, which is what the endpoint would have given the merchant.

    Does NOT append to the ledger, unlike services.create_policy. An append
    advances the tenant's chain sequence, so a fixture that appended would
    renumber the money chain and make every test asserting on `seq` depend on
    unrelated setup.
    """
    policy = StorePolicy.objects.create(merchant=merchant, name=name, is_active=active)
    seed_rules(policy)

    for kind, spec in rules.items():
        default_on_breach = REGISTRY[kind].default_on_breach
        config = dict(spec or {})
        state = config.pop("state", RuleState.ENFORCING)
        on_breach = config.pop("on_breach", default_on_breach)
        updated = PolicyRule.objects.filter(policy=policy, kind=kind).update(
            config=config, state=state, on_breach=on_breach,
        )
        assert updated == 1, f"seed_rules did not create a {kind} row"

    return policy
