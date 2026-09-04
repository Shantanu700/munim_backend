import os
import re

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application

django_app = get_asgi_application()

from agentmcp.server import build_mcp_app

mcp_app = build_mcp_app()

_SCOPED_MCP_RE = re.compile(r"^/mcp/([^/]+)$")

_SCOPE_HEADER = b"x-munim-mcp-domain"


def _with_scope_header(headers, domain: str | None):
    filtered = [(k, v) for k, v in (headers or []) if k.lower() != _SCOPE_HEADER]
    if domain is not None:
        filtered.append((_SCOPE_HEADER, domain.encode("utf-8")))
    return filtered


async def _not_found(send):
    await send({"type": "http.response.start", "status": 404,
               "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"not found"})


async def application(scope, receive, send):
    """Route /mcp to the MCP server, everything else to Django.

    Two doors share the same MCP app:

    - /mcp (bare) is the platform-wide door -- forwarded unmodified, no
      domain header set. agentmcp/auth.py gates it on an API key.
    - /mcp/<domain> is a merchant's own scoped door -- the domain is peeled
      off the path and carried as a header instead, and the path is rewritten
      back to /mcp because the MCP SDK registers a fixed route, not a prefix
      mount (see build_mcp_app). This app never sees the extra segment.

    Deliberately not starlette.routing.Mount: Mount("/mcp") compiles to
    ^/mcp/(?P<path>.*)$ and does NOT match a bare /mcp, which is the exact URL
    every MCP client is configured with. This dispatch matches both, plus the
    per-merchant shape.

    Lifespan goes only to the MCP app -- its streamable-HTTP session manager
    starts there, and Django's ASGIHandler does not implement lifespan at all.
    """
    if scope["type"] == "lifespan":
        await mcp_app(scope, receive, send)
        return

    path = scope.get("path", "")

    if path == "/mcp":
        scope = {**scope, "headers": _with_scope_header(scope.get("headers"), None)}
        await mcp_app(scope, receive, send)
        return

    if path.startswith("/mcp/"):
        match = _SCOPED_MCP_RE.match(path)
        if match is None:
            await _not_found(send)
            return
        domain = match.group(1)
        scope = {
            **scope,
            "path": "/mcp",
            "raw_path": b"/mcp",
            "headers": _with_scope_header(scope.get("headers"), domain),
        }
        await mcp_app(scope, receive, send)
        return

    await django_app(scope, receive, send)
