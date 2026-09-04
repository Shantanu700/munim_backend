"""The merchant-facing MCP server.

Mounted into the Django ASGI app at /mcp (see config/asgi.py) so the MCP
endpoint and the Razorpay webhook share one process, one port, one tunnel.
"""
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from agentmcp import auth, catalog
from agentmcp import checkout as checkout_core
from agentmcp.clients import identify
from agentmcp.schemas import (
    CheckoutResult,
    MerchantOut,
    MerchantResult,
    MultiSearchResult,
    ProductOut,
    SearchResult,
)
from modules.twin.choices import CatalogReadTool
from modules.twin.models import CatalogRead

log = logging.getLogger(__name__)

mcp = MCPServer("munim")


def _threaded(fn, *args, **kwargs):
    return sync_to_async(fn, thread_sensitive=True)(*args, **kwargs)


def _count_read(tenants, tool, label: str) -> None:
    """Count a catalogue read, and never let counting break the read.

    A merchant's analytics number is worth less than an agent's search working,
    so every failure path -- a dead connection, a migration not yet applied, a
    tool name that is not in CatalogReadTool -- is logged and swallowed. Called
    AFTER the rows have been assembled, so a counter that cannot be written
    cannot also hide products.

    Not inside catalog.py: that module is the read path and stays a pure read,
    which is what makes it testable with no write side effects. The MCP tool is
    where the caller's identity is known, so it is where attribution belongs --
    the same placement checkout() already uses for identify().
    """
    try:
        for tenant in tenants:
            CatalogRead.record(tenant, tool, label)
    except Exception:
        log.exception("catalogue read counter failed for %s; read served anyway", tool)


def _resolve_tenant_for_call(ctx: Context, merchant: str | None):
    """Which store a tool call is about, and whether it is allowed to ask.

    Two doors onto the same tools: a merchant-scoped /mcp/<domain> URL locks
    the tenant to the URL (a conflicting `merchant` argument is refused, not
    silently overridden), while the bare platform /mcp requires a valid API
    key and leaves `merchant` as the caller's choice.
    """
    domain = catalog.scoped_domain(ctx.headers)
    if domain is not None:
        return catalog.resolve_scoped_tenant(ctx.headers, merchant)
    auth.resolve_api_key(ctx.headers)
    return catalog.resolve_tenant(merchant)


@mcp.tool()
def ping(name: str) -> str:
    """Verify connectivity. Returns a greeting."""
    return f"pong {name}"


@mcp.tool()
async def search_products(
    ctx: Context,
    query: str = "",
    limit: int = 10,
    max_price_rupees: float | None = None,
    in_stock_only: bool = True,
    merchant: str | None = None,
) -> SearchResult:
    """Search a merchant's catalog.

    All words in `query` must match; leave it empty to browse. Use
    `max_price_rupees` to stay inside a spending limit.

    Prices here are a snapshot taken when the catalog was ingested from the
    store's own page markup. Checkout re-reads the live product page and will
    refuse with PRICE_CHANGED if the price has moved since -- that is expected,
    not a fault. Re-quote at the price checkout reports.

    On a merchant-scoped endpoint, `merchant` is ignored if it names that same
    store and refused if it names a different one -- this endpoint is locked
    to one store. On the platform-wide endpoint, `merchant` picks which store
    to search (an API key is required); use search_all_merchants to compare
    across stores instead.
    """
    label, _raw = identify(ctx.headers)

    def _run():
        tenant = _resolve_tenant_for_call(ctx, merchant)
        max_paise = None if max_price_rupees is None else int(round(max_price_rupees * 100))
        rows = catalog.search(tenant, query=query, limit=limit,
                              max_price_paise=max_paise, in_stock_only=in_stock_only)
        result = SearchResult(merchant=tenant.domain, count=len(rows),
                              products=[ProductOut.of(r) for r in rows])
        _count_read([tenant], CatalogReadTool.SEARCH_PRODUCTS, label)
        return result

    try:
        return await _threaded(_run)
    except (catalog.CatalogError, auth.AuthError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def get_product(
    ctx: Context,
    sku: str | None = None,
    url: str | None = None,
    merchant: str | None = None,
) -> ProductOut:
    """Fetch one product by SKU or by product page URL. Pass exactly one.

    As with search, the price is the catalog snapshot; checkout re-verifies it
    against the live page before any money moves. See search_products for how
    `merchant` behaves on a merchant-scoped vs the platform-wide endpoint.
    """
    label, _raw = identify(ctx.headers)

    def _run():
        tenant = _resolve_tenant_for_call(ctx, merchant)
        result = ProductOut.of(catalog.get(tenant, sku=sku, url=url))
        _count_read([tenant], CatalogReadTool.GET_PRODUCT, label)
        return result

    try:
        return await _threaded(_run)
    except (catalog.CatalogError, auth.AuthError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def list_merchants(ctx: Context) -> MerchantResult:
    """List every merchant onboarded to the platform.

    Only meaningful on the platform-wide endpoint (requires an API key); a
    merchant-scoped endpoint is already locked to one store.
    """
    def _run():
        if catalog.scoped_domain(ctx.headers) is not None:
            raise catalog.CatalogError(
                "list_merchants is only meaningful on the platform-wide endpoint; "
                "this endpoint is already locked to one merchant."
            )
        auth.resolve_api_key(ctx.headers)
        rows = catalog.list_merchants()
        return MerchantResult(
            count=len(rows),
            merchants=[MerchantOut(domain=m.domain or "", business_name=m.business_name or "")
                      for m in rows],
        )

    try:
        return await _threaded(_run)
    except (catalog.CatalogError, auth.AuthError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def search_all_merchants(
    ctx: Context,
    query: str = "",
    limit: int = 10,
    max_price_rupees: float | None = None,
    in_stock_only: bool = True,
    merchants: list[str] | None = None,
) -> MultiSearchResult:
    """Search across every onboarded merchant, or a caller-chosen subset.

    Pass `merchants` (a list of domains) to narrow this one call to those
    stores -- it is a per-query filter, not a saved allowlist; omit it to
    search everyone. To buy, follow up with `checkout` naming the one merchant
    you chose. Only meaningful on the platform-wide endpoint (requires an API
    key); a merchant-scoped endpoint is already locked to one store, so use
    search_products there instead.
    """
    label, _raw = identify(ctx.headers)

    def _run():
        if catalog.scoped_domain(ctx.headers) is not None:
            raise catalog.CatalogError(
                "search_all_merchants is only meaningful on the platform-wide endpoint; "
                "use search_products here, it is already scoped to this one merchant."
            )
        auth.resolve_api_key(ctx.headers)
        max_paise = None if max_price_rupees is None else int(round(max_price_rupees * 100))
        searched = catalog.tenants_for(merchants)
        by_domain = catalog.search_all(query=query, limit=limit, max_price_paise=max_paise,
                                       in_stock_only=in_stock_only, domains=merchants)
        by_merchant = {domain: [ProductOut.of(p) for p in rows] for domain, rows in by_domain.items()}
        result = MultiSearchResult(count=sum(len(v) for v in by_merchant.values()),
                                   by_merchant=by_merchant)
        _count_read(searched, CatalogReadTool.SEARCH_ALL_MERCHANTS, label)
        return result

    try:
        return await _threaded(_run)
    except (catalog.CatalogError, auth.AuthError) as exc:
        raise ToolError(str(exc)) from exc


def build_mcp_app():
    """Starlette app serving MCP at /mcp.

    config.asgi dispatches on the path prefix rather than mounting, so this app
    keeps its own full /mcp path. It also rewrites /mcp/<domain> back to /mcp
    before forwarding, stashing <domain> in a header instead -- this app only
    ever sees the one path below, for both the merchant-scoped and the
    platform-wide door.

    transport_security must be explicit: the SDK defaults to a 127.0.0.1-only
    allowlist and answers a public Host: header with 421 (handoff §4). It is
    host/origin-based, not path-based, so it needs no change for the new
    /mcp/<domain> shape -- confirmed, not just assumed.
    """
    host = settings.PUBLIC_HOST
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{host}:*", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[
                f"https://{host}",
                "http://localhost:*",
                "https://chatgpt.com",
                "https://chat.openai.com",
            ],
        ),
    )


@mcp.tool()
async def checkout(
    ctx: Context,
    mandate: str,
    sku: str | None = None,
    url: str | None = None,
    quantity: int = 1,
    quoted_price_paise: int | None = None,
    merchant: str | None = None,
) -> CheckoutResult:
    """Buy something. Requires a signed spending mandate.

    Pass the `mandate` token the buyer issued you, and identify the item by
    `sku` or `url`. `quoted_price_paise` is the price you showed the buyer;
    leave it out to accept the catalog price.

    The live product page is re-read before anything is charged, so act on the
    verdict you get back:

    - ALLOW: `payment_url` is a real Razorpay link on the MERCHANT's own
      account. Give it to the buyer. Payment confirms by webhook.
    - PRICE_CHANGED with verdict DENY: the price moved since the catalog was
      built. No human is needed -- tell the buyer the new price and call
      checkout again with `quoted_price_paise` set to `live_price_paise`.
    - STEP_UP: a human has to approve this one, and `message` says why -- it is
      not always a spending limit. Show `approval_url` and wait; do not retry,
      and do not re-quote, it will keep returning the same thing until a human
      acts. A store that tolerates small price rises returns PRICE_CHANGED as a
      STEP_UP rather than a DENY, so read the verdict, not the reason code.
    - Any other DENY is final. Do not retry it.

    Nothing is charged by calling this. It creates a payment link; the buyer
    still has to pay it.
    """
    label, raw = identify(ctx.headers)

    def _run():
        tenant = _resolve_tenant_for_call(ctx, merchant)
        outcome = checkout_core.quote_and_gate(
            tenant, mandate_token=mandate, sku=sku, url=url,
            quantity=quantity, quoted_price_paise=quoted_price_paise,
            llm_client=label, llm_client_raw=raw,
        )
        return CheckoutResult(
            verdict=outcome.verdict, reason_code=outcome.reason_code,
            message=outcome.message, payment_url=outcome.payment_url,
            approval_url=outcome.approval_url, amount_paise=outcome.amount_paise,
            live_price_paise=outcome.live_price_paise, sku=outcome.sku,
            title=outcome.title, ledger_seq=outcome.ledger_seq,
        )

    try:
        return await _threaded(_run)
    except (catalog.CatalogError, auth.AuthError) as exc:
        raise ToolError(str(exc)) from exc
