"""Platform API key verification for the bare /mcp endpoint.

Only the platform-wide door needs this: a merchant-scoped /mcp/<domain> call
is gated by nothing here (see catalog.resolve_scoped_tenant) -- the merchant
chose to publish that URL as public storefront surface, and checkout is
separately gated by the buyer's mandate regardless of which door it came
through. The platform door is the one that can enumerate every merchant and
compare pricing across stores, so it needs to be attributable and revocable.
"""
from __future__ import annotations

from django.utils import timezone

from modules.core.models import PlatformAPIKey


class AuthError(Exception):
    """Raised when the platform endpoint cannot identify the caller."""


def _bearer_token(headers) -> str | None:
    headers = headers or {}
    print(headers)
    raw = str(headers.get("authorization", ""))
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[len("bearer "):].strip()
    return token or None


def resolve_api_key(headers) -> PlatformAPIKey:
    """Verify the Authorization header on a platform-mode call. Raises AuthError."""
    token = _bearer_token(headers)
    if not token:
        raise AuthError(
            "Missing bearer token. Platform-wide MCP calls require "
            "Authorization: Bearer <your platform API key>. Issue one via "
            "POST /core/api-keys/, or use this merchant's own /mcp/<domain> "
            "URL if you only need one store."
        )
    key = PlatformAPIKey.resolve(token)
    if key is None:
        raise AuthError("Invalid or revoked API key.")
    PlatformAPIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key
