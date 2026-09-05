"""Order and payment reads behind the MCP tools.

Plain synchronous Django, like catalog.py and for the same reason: the MCP layer
wraps these in sync_to_async, so this module stays ordinary code that can be
tested without any async machinery.

The interesting problem here is WHO IS ASKING. The MCP server is stateless and
has no session; a tool sees headers and arguments and nothing else. Headers
cannot answer it -- the platform API key is a gate, not an identity, and the
scoped-domain header names a store rather than a person. The only thing that
names a buyer is the mandate they signed, which is why every read below takes a
token and verifies it rather than trusting anything cheaper.
"""
from __future__ import annotations

from agentmcp.catalog import CatalogError
from modules.policy import jws
from modules.policy.models import Mandate
from modules.twin.orders import Order

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def buyer_from_mandate(tenant, mandate_token: str):
    """The Buyer a signed mandate belongs to, on this store.

    Repeats what engine.evaluate does at the top of a checkout -- verify the
    signature, find the row by jti, confirm it is this tenant's -- because the
    property being relied on is the same one: a token is only worth what its
    signature proves, and the mandate row's own columns are a convenience copy.

    Deliberately does NOT reject an expired or revoked mandate. Those bar new
    SPENDING, which is engine.evaluate's job at checkout and at retry. Reading
    what you already bought is not spending, and a buyer whose mandate lapsed
    last night still needs to see last night's orders. What must not happen is a
    mandate reading ACROSS stores, and the tenant check below is that.
    """
    if not mandate_token:
        raise CatalogError("A signed mandate is required to see a buyer's orders.")

    try:
        claims = jws.verify(mandate_token)
    except jws.MandateInvalid as exc:
        raise CatalogError(f"That mandate is not usable: {exc.detail or exc.reason}") from exc

    jti = claims.get("jti")
    mandate = Mandate.objects.select_related("buyer").filter(jti=jti).first() if jti else None
    if mandate is None:
        raise CatalogError("That mandate is not one this platform issued.")
    if mandate.tenant_id != tenant.id:
        raise CatalogError(
            f"That mandate was issued for a different store, not {tenant.domain}.")
    if mandate.buyer is None:
        raise CatalogError("That mandate is not linked to a buyer.")
    return mandate.buyer


def _visible(tenant, buyer):
    """Every order this buyer placed on this store.

    Scoped to the BUYER rather than to the presenting mandate, so a mandate
    issued this morning still shows what an earlier one bought last week --
    which is what "what has my agent been spending" has to mean, and mandates
    are short-lived by design. Never scoped wider than the tenant: reading one
    store's orders through a mandate held for another is the isolation every
    other query in this codebase maintains.
    """
    return (Order.objects.filter(tenant=tenant, buyer=buyer)
            .select_related("mandate")
            .prefetch_related("payments"))


def list_for_buyer(tenant, mandate_token: str, *, status: str | None = None,
                   limit: int = DEFAULT_LIMIT) -> list[Order]:
    """This buyer's orders on this store, newest first."""
    buyer = buyer_from_mandate(tenant, mandate_token)
    rows = _visible(tenant, buyer)

    if status:
        wanted = status.strip().lower()
        valid = {s.value for s in Order.Status}
        if wanted not in valid:
            raise CatalogError(
                f"Unknown status {status!r}. Use one of: {', '.join(sorted(valid))}.")
        rows = rows.filter(status=wanted)

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    return list(rows[:limit])


def get_for_buyer(tenant, mandate_token: str, order_number: str) -> Order:
    """One of this buyer's orders, by its human order number.

    Looked up by order_number because that is the handle the agent was given
    back at checkout and the one a buyer can read off a receipt. It is unique
    per tenant, and the query is already scoped to one buyer inside one tenant,
    so it cannot be walked to anyone else's order -- an order that exists but
    belongs to someone else is simply not found, which is also what the buyer's
    REST surface does with 404.
    """
    buyer = buyer_from_mandate(tenant, mandate_token)
    number = (order_number or "").strip()
    if not number:
        raise CatalogError("An order number is required.")

    order = _visible(tenant, buyer).filter(order_number__iexact=number).first()
    if order is None:
        raise CatalogError(f"No order {number!r} for this buyer on {tenant.domain}.")
    return order


def get_retryable(tenant, mandate_token: str, order_number: str) -> Order:
    """The order a retry is about, resolved the same way a read is.

    Separate from get_for_buyer only so the retry path reads as its own step;
    the eligibility rules -- including that the presented mandate must be the
    one that authorised the order -- live in checkout.check_retryable, next to
    the code that acts on them.
    """
    return get_for_buyer(tenant, mandate_token, order_number)
