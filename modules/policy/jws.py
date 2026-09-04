"""Ed25519-signed mandates, as compact JWS.

A JWS is three base64url segments joined by dots -- header.payload.signature --
where the signature covers the ASCII bytes of "header.payload" exactly as they
were transmitted. It is not encryption: the payload is world-readable. It is
tamper-evidence, which is the point, because the party holding the mandate is
the party it constrains. The agent cannot raise its own spending cap.

Same byte-exactness rule as the Razorpay webhook in twin/views.py: verify the
bytes you received. json.dumps(json.loads(payload)) breaks a JWS for exactly
the reason it breaks that HMAC.

This module imports no models, so it can be tested with no database.
"""
from __future__ import annotations

import base64
import json

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from modules.policy.reasons import ReasonCode

ALG = "EdDSA"
CRV = "Ed25519"


class MandateInvalid(Exception):
    """Carries the ReasonCode the engine should record, so failures need no
    translation layer between crypto and policy."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def b64u(raw: bytes) -> str:
    """base64url with padding stripped, per RFC 7515 section 2."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    """Re-pad before decoding. Forgetting this is the classic JWS bug."""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, str(exc)) from exc


def _keys() -> dict[str, bytes]:
    keys = getattr(settings, "MANDATE_KEYS", {})
    if not keys:
        raise ImproperlyConfigured(
            "No mandate signing keys. Run `python manage.py mandate_keygen` and put "
            "the printed MUNIM_MANDATE_KEYS line in your .env."
        )
    return keys


def active_kid() -> str:
    kid = getattr(settings, "MANDATE_ACTIVE_KID", "")
    if not kid:
        _keys()
    return kid


def public_key_b64(kid: str) -> str:
    """The verifying half, safe to publish. This is what lets a merchant or an
    auditor check a mandate without trusting Munim's database."""
    return base64.b64encode(bytes(SigningKey(_keys()[kid]).verify_key)).decode()


def sign(claims: dict, kid: str | None = None) -> str:
    kid = kid or active_kid()
    keys = _keys()
    if kid not in keys:
        raise ImproperlyConfigured(f"No mandate key with kid {kid!r}")

    header = {"alg": ALG, "crv": CRV, "typ": "JWS", "kid": kid}
    header_b64 = b64u(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    payload_b64 = b64u(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = SigningKey(keys[kid]).sign(signing_input).signature
    return f"{header_b64}.{payload_b64}.{b64u(signature)}"


def verify(token: str) -> dict:
    """Return the claims, or raise MandateInvalid. Nothing downstream may read a
    claim that has not been through here."""
    if not token or not isinstance(token, str):
        raise MandateInvalid(ReasonCode.MANDATE_MISSING)

    parts = token.split(".")
    if len(parts) != 3:
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, f"{len(parts)} segments")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(unb64u(header_b64))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, "header not JSON") from exc
    if not isinstance(header, dict):
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, "header not an object")

    if header.get("alg") != ALG:
        raise MandateInvalid(ReasonCode.MANDATE_ALG_UNSUPPORTED, repr(header.get("alg")))

    kid = header.get("kid")
    keys = _keys()
    if kid not in keys:
        raise MandateInvalid(ReasonCode.MANDATE_UNKNOWN_KEY, repr(kid))

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        VerifyKey(bytes(SigningKey(keys[kid]).verify_key)).verify(
            signing_input, unb64u(signature_b64)
        )
    except BadSignatureError as exc:
        raise MandateInvalid(ReasonCode.MANDATE_BAD_SIGNATURE) from exc

    try:
        claims = json.loads(unb64u(payload_b64))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, "payload not JSON") from exc
    if not isinstance(claims, dict):
        raise MandateInvalid(ReasonCode.MANDATE_MALFORMED, "payload not an object")

    return claims
