"""Razorpay Payment Links, on the merchant's own account.

One endpoint, one POST. The razorpay SDK would be a dependency for a single
call that stdlib makes in twenty lines -- the same shape twin/ingest.py already
uses to fetch a page.

Credentials come from the Merchant, never from settings. A silent fallback to a
platform-wide account would route a buyer's money to the wrong party.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1/payment_links"

PROBE_API = "https://api.razorpay.com/v1/payments?count=1"

TIMEOUT_S = 20
VERIFY_TIMEOUT_S = 8

MAX_DESCRIPTION = 2048
MAX_REFERENCE_ID = 40
MAX_NOTES = 15


class PaymentError(Exception):
    """Carries the response body: a payment failure has to be diagnosable."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        self.status = status
        self.body = body
        super().__init__(message)


def _auth_header(key_id: str, key_secret: str) -> str:
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    return f"Basic {token}"


def _request(method: str, url: str, key_id: str, key_secret: str,
             payload: dict | None = None, timeout: int = TIMEOUT_S) -> dict:
    """One authenticated call. Raises PaymentError; never logs the credentials."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": _auth_header(key_id, key_secret)},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        log.error("razorpay %s %s -> %s: %s", method, urllib.parse.urlparse(url).path,
                  exc.code, detail)
        raise PaymentError(f"Razorpay refused the request ({exc.code})",
                           status=exc.code, body=detail) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PaymentError(f"Could not reach Razorpay: {exc}") from exc


def key_mode(key_id: str) -> str | None:
    """'live', 'test', or None -- the prefix is a key's only self-description."""
    for mode in ("live", "test"):
        if (key_id or "").startswith(f"rzp_{mode}_"):
            return mode
    return None


def verify_credentials(key_id: str, key_secret: str) -> dict:
    """Prove a key pair actually authenticates against Razorpay.

    Returns {ok, account_id, business_name, mode, source, message}. Never raises for
    a credential problem -- a rejected key is an answer, not an error.

    Razorpay offers no "who am I" call on an ordinary key, so the plain probe below
    is what decides ok. account_id/business_name stay None: the Partner accounts API
    that could fill them in is for sub-merchants, which we don't have.
    """
    result = {"ok": False, "account_id": None, "business_name": None,
              "mode": key_mode(key_id), "source": None, "message": ""}

    if not key_id or not key_secret:
        result["message"] = "Both a key id and a key secret are required."
        return result

    try:
        _request("GET", PROBE_API, key_id, key_secret, timeout=VERIFY_TIMEOUT_S)
    except PaymentError as exc:
        if exc.status in (400, 401, 403):
            result["message"] = "Razorpay rejected these credentials."
        else:
            result["message"] = str(exc)
        return result

    result.update(
        ok=True,
        source="probe",
        message="These credentials authenticate. Razorpay does not expose the account "
                "name to a standard key, so the store name shown is the one on file here.",
    )
    return result


def create_payment_link(merchant, *, amount_paise: int, description: str,
                        reference_id: str, notes: dict, expire_minutes: int = 30) -> dict:
    """Create a link on `merchant`'s Razorpay account. Returns the response dict."""
    if not merchant.is_payment_ready:
        raise PaymentError(f"{merchant.domain} has no Razorpay credentials on file")
    if amount_paise <= 0:
        raise PaymentError(f"amount must be positive, got {amount_paise}")
    if len(notes) > MAX_NOTES:
        raise PaymentError(f"Razorpay allows {MAX_NOTES} notes, got {len(notes)}")

    payload = {
        "amount": int(amount_paise),
        "currency": "INR",
        "description": description[:MAX_DESCRIPTION],
        "reference_id": reference_id[:MAX_REFERENCE_ID],
        "expire_by": int(time.time()) + expire_minutes * 60,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {k: str(v) for k, v in notes.items()},
    }

    body = _request("POST", API, merchant.razorpay_key_id, merchant.razorpay_key_secret,
                    payload=payload)

    if not isinstance(body, dict):
        raise PaymentError("Razorpay returned an unexpected response",
                           body=json.dumps(body)[:500])
    if not body.get("short_url"):
        raise PaymentError("Razorpay returned no short_url", body=json.dumps(body)[:500])
    return body
