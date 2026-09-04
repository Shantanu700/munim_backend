"""Catalog reads behind the MCP tools.

Plain synchronous Django. The MCP layer wraps these in sync_to_async, so this
module stays ordinary code that can be tested without any async machinery.
"""
from __future__ import annotations

from modules.core.models import Merchant
from modules.twin.models import Product

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


class CatalogError(Exception):
    """Something the agent asked for does not exist or is ambiguous."""


def resolve_tenant(domain: str | None = None) -> Merchant:
    """Which store this call is about.

    Same rule twin/views.py uses for webhooks: an explicit name wins, and a
    single-merchant install needs no name at all. Per-merchant MCP endpoints
    would remove the guessing entirely, but with one store in play they buy
    nothing.
    """
    if domain:
        tenant = Merchant.objects.filter(domain=domain).first()
        if tenant is None:
            known = ", ".join(Merchant.objects.values_list("domain", flat=True)) or "none"
            raise CatalogError(f"No merchant {domain!r}. Known merchants: {known}")
        return tenant

    merchants = list(Merchant.objects.all()[:2])
    if not merchants:
        raise CatalogError("No merchants have been onboarded yet.")
    if len(merchants) > 1:
        known = ", ".join(Merchant.objects.values_list("domain", flat=True))
        raise CatalogError(f"Several merchants exist; name one of: {known}")
    return merchants[0]


def search(tenant, query: str = "", limit: int = DEFAULT_LIMIT,
           max_price_paise: int | None = None, in_stock_only: bool = True) -> list[Product]:
    """Catalog rows matching every term in `query`.

    Terms are ANDed, not ORed: "green co-ord" should narrow rather than widen,
    which is what someone typing two words means. A blank query browses.
    """
    rows = Product.objects.filter(tenant=tenant, is_visible_to_agents=True)
    if in_stock_only:
        rows = rows.filter(in_stock=True)
    if max_price_paise is not None:
        rows = rows.filter(price_paise__lte=max_price_paise)

    for term in (query or "").split():
        rows = rows.filter(title__icontains=term) | rows.filter(sku__icontains=term)
        rows = rows.filter(tenant=tenant)   

    limit = DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(1, min(limit, MAX_LIMIT))
    return list(rows.distinct().order_by("-confidence", "price_paise")[:limit])


SCOPE_HEADER = "x-munim-mcp-domain"


def scoped_domain(headers) -> str | None:
    """The merchant this call is hard-locked to, if any.

    Set by config.asgi when the request arrived at /mcp/<domain>; absent on
    the bare platform /mcp. Tool code must treat this, not the `merchant`
    argument, as authoritative once it is present -- see resolve_scoped_tenant.
    """
    headers = headers or {}
    return headers.get(SCOPE_HEADER) or None


def resolve_scoped_tenant(headers, merchant_arg: str | None = None) -> Merchant:
    """Tenant for a call that arrived through /mcp/<domain>.

    The domain in the URL always wins. A conflicting `merchant` argument is an
    error, not a silent override -- an LLM must not be able to talk its way
    into a different merchant's catalog than the URL it was configured with.
    """
    domain = scoped_domain(headers)
    if merchant_arg and merchant_arg != domain:
        raise CatalogError(
            f"This MCP endpoint is locked to {domain!r}; it cannot search or check out "
            f"at {merchant_arg!r}. Use the platform-wide endpoint for cross-merchant access."
        )
    return resolve_tenant(domain)


def list_merchants() -> list[Merchant]:
    """Every onboarded merchant. Platform mode only."""
    return list(Merchant.objects.all().order_by("domain"))


def tenants_for(domains: list[str] | None = None) -> list[Merchant]:
    """The stores a cross-merchant search will read.

    Extracted from search_all so a caller can attribute the read without
    recomputing the filter. server._count_read needs the stores that were
    SEARCHED, and search_all's return value only names the ones that had a hit:
    a merchant with a thin or badly-titled catalogue is exactly the one who
    needs to know agents are looking and finding nothing.
    """
    tenants = list(Merchant.objects.all())
    if domains:
        wanted = set(domains)
        tenants = [t for t in tenants if t.domain in wanted]
    return tenants


def search_all(query: str = "", limit: int = DEFAULT_LIMIT,
               max_price_paise: int | None = None, in_stock_only: bool = True,
               domains: list[str] | None = None) -> dict[str, list[Product]]:
    """Cross-merchant search for platform mode, keyed by merchant domain.

    Loops search() per tenant rather than building one cross-tenant queryset:
    search()'s per-term AND-matching resets to `.filter(tenant=tenant)` after
    every OR specifically to keep the union from widening tenant scope, and
    that trick does not generalise to "OR across N tenants" without either
    duplicating the query-building logic or risking exactly the leak that
    comment exists to prevent. At hackathon merchant counts, N small reused
    queries is simpler and safer than a new cross-tenant query builder.

    `domains`, when given, is a filter for THIS call only -- not a stored
    allowlist. Unknown names in it are silently skipped.
    """
    tenants = tenants_for(domains)

    results: dict[str, list[Product]] = {}
    for tenant in tenants:
        rows = search(tenant, query=query, limit=limit,
                      max_price_paise=max_price_paise, in_stock_only=in_stock_only)
        if rows:
            results[tenant.domain] = rows
    return results


def get(tenant, sku: str | None = None, url: str | None = None) -> Product:
    if bool(sku) == bool(url):
        raise CatalogError("Pass exactly one of sku or url.")

    lookup = {"sku": sku} if sku else {"url": url}
    product = Product.objects.filter(tenant=tenant, is_visible_to_agents=True, **lookup).first()
    if product is None:
        what = f"sku {sku!r}" if sku else f"url {url!r}"
        raise CatalogError(f"No product with {what} in {tenant.domain}'s catalog.")
    return product
