"""Every mutation to a merchant's Razorpay linkage, in one place.

Same rule as modules/policy/services.py: a serializer's save() is the wrong home
for these, because each one is a small cross-field state machine (saving keys
unverifies them; minting a secret invalidates the one in Razorpay's dashboard) and
because the ledger append belongs at the call site, not in a signal where nobody
reading the view can see it.

Nothing here ever puts a secret in the ledger, in a log line, or in an exception
message. The ledger is readable at /audit/verify, and the whole point of encrypting
the columns is lost if the plaintext is copied into a JSONField next to them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
import uuid

from django.db import transaction
from django.utils import timezone

from modules.core.models import Merchant
from modules.ledger.models import LedgerEntry
from payments import razorpay

log = logging.getLogger(__name__)

__all__ = [
    "TEST_EVENT_TYPE",
    "WEBHOOK_EVENTS",
    "WEBHOOK_SECRET_PREFIX",
    "mask_key_id",
    "rotate_webhook_secret",
    "save_credentials",
    "send_test_event",
    "verify_and_record",
    "webhook_arrival",
]

WEBHOOK_SECRET_PREFIX = "whsec_"

WEBHOOK_EVENTS = [
    {"event": "payment_link.paid", "required": True,
     "why": "Settles the order. Without it a paid order is never marked paid."},
    {"event": "payment.captured", "required": False,
     "why": "Stored for diagnostics; not counted, or one purchase would count twice."},
    {"event": "payment.failed", "required": False,
     "why": "Stored so a failed attempt is visible in the event log."},
]

TEST_EVENT_TYPE = "munim.webhook_test"

TEST_EVENT_TIMEOUT_S = 10

TEST_EVENT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126 Safari/537.36")


def mask_key_id(key_id: str | None) -> str | None:
    """rzp_live_QK3nZ8pXvT2mLd -> rzp_live_...T2mLd. Safe for a ledger entry.

    Fixed lengths, not a split on the last underscore. Masking relative to a
    delimiter reveals however much happens to precede it, and this function is the
    last thing standing between a value someone pasted into the wrong box and the
    ledger -- which is hash-chained and append-only, so a secret written there
    cannot be scrubbed without breaking /audit/verify for that tenant.

    So: keep the recognised rzp_live_/rzp_test_ prefix if there is one, keep five
    trailing characters, and reveal nothing else regardless of the input's shape.
    """
    if not key_id:
        return None
    for mode in ("live", "test"):
        prefix = f"rzp_{mode}_"
        if key_id.startswith(prefix):
            return f"{prefix}...{key_id[-5:]}"
    return f"...{key_id[-4:]}"


def _lock(merchant):
    """Take the Merchant row lock, then re-read into the caller's instance.

    Same lock, same order as LedgerEntry.append takes it, so a write here and a
    concurrent append cannot deadlock against each other -- the reasoning
    modules/policy/services.py spells out. The refresh matters independently: these
    are read-modify-writes on an instance the view loaded before the lock existed.
    """
    Merchant.objects.select_for_update().get(pk=merchant.pk)
    merchant.refresh_from_db()
    return merchant


@transaction.atomic
def save_credentials(merchant, *, key_id=None, key_secret=None, webhook_secret=None,
                     by_user=None):
    """Store what the merchant typed. Partial: None means "leave alone"."""
    _lock(merchant)
    fields = []
    if key_id is not None:
        merchant.razorpay_key_id = key_id
        fields.append("razorpay_key_id")
    if key_secret is not None:
        merchant.razorpay_key_secret = key_secret
        fields.append("razorpay_key_secret")
    if webhook_secret is not None:
        merchant.razorpay_webhook_secret = webhook_secret
        fields.append("razorpay_webhook_secret")

    if not fields:
        return merchant

    if {"razorpay_key_id", "razorpay_key_secret"} & set(fields):
        merchant.razorpay_verified_at = None
        merchant.razorpay_account_id = None
        merchant.razorpay_account_name = None
        fields += ["razorpay_verified_at", "razorpay_account_id", "razorpay_account_name"]

    merchant.save(update_fields=fields)

    LedgerEntry.append(
        tenant=merchant, action="razorpay_credentials_saved", decision="ALLOW",
        reason_code="MERCHANT_UPDATE",
        detail={"by_user": getattr(by_user, "email", None),
                "key_id": mask_key_id(merchant.razorpay_key_id),
                "key_mode": merchant.razorpay_key_mode,
                "fields": sorted(set(fields))},
    )
    return merchant


def verify_and_record(merchant, *, key_id=None, key_secret=None, by_user=None) -> dict:
    """Check a key pair against Razorpay, recording success on the merchant.

    key_id/key_secret override what is stored, so the UI can verify a pair before
    committing it. The network call is deliberately outside the transaction that
    records the result -- holding a row lock open across someone else's API is how
    a slow third party becomes your outage.
    """
    key_id = key_id or merchant.razorpay_key_id or ""
    key_secret = key_secret or merchant.razorpay_key_secret or ""

    result = razorpay.verify_credentials(key_id, key_secret)

    with transaction.atomic():
        _lock(merchant)
        verified_what_is_stored = (
            key_id == (merchant.razorpay_key_id or "")
            and key_secret == (merchant.razorpay_key_secret or "")
        )
        if result["ok"] and verified_what_is_stored:
            merchant.razorpay_verified_at = timezone.now()
            merchant.razorpay_account_id = result.get("account_id")
            merchant.razorpay_account_name = result.get("business_name")
            merchant.save(update_fields=["razorpay_verified_at", "razorpay_account_id",
                                         "razorpay_account_name"])

        LedgerEntry.append(
            tenant=merchant, action="razorpay_credentials_verified",
            decision="ALLOW" if result["ok"] else "DENY",
            reason_code="VERIFIED" if result["ok"] else "REJECTED",
            detail={"by_user": getattr(by_user, "email", None),
                    "key_id": mask_key_id(key_id), "key_mode": result.get("mode"),
                    "source": result.get("source"),
                    "account_id": result.get("account_id")},
        )
    return result


@transaction.atomic
def rotate_webhook_secret(merchant, *, by_user=None) -> str:
    """Mint the shared secret and return it in the clear.

    Unlike PlatformAPIKey, this one cannot be hash-only. Razorpay does not issue it
    -- the merchant invents it when creating the webhook, and both sides must hold
    the same string forever after. A merchant who loses it needs to read it back,
    not mint a new one and re-key the dashboard.
    """
    _lock(merchant)
    had_one = bool(merchant.razorpay_webhook_secret)
    merchant.razorpay_webhook_secret = WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(24)
    merchant.save(update_fields=["razorpay_webhook_secret"])

    LedgerEntry.append(
        tenant=merchant, action="razorpay_webhook_secret_rotated", decision="ALLOW",
        reason_code="ROTATED" if had_one else "CREATED",
        detail={"by_user": getattr(by_user, "email", None), "replaced": had_one},
    )
    return merchant.razorpay_webhook_secret


def webhook_arrival(merchant) -> dict:
    """Has a signature-verified webhook ever reached this store's endpoint?

    Imported here rather than at module scope: modules.twin imports modules.core for
    TenantScoped, so a top-level import would close the cycle.
    """
    from modules.twin.models import Event

    last = (Event.objects.filter(tenant=merchant, signature_valid=True)
            .order_by("-received_at").first())
    if last is None:
        return {"webhook_received": False, "last_event_type": None,
                "last_event_at": None, "last_event_was_test": False}
    return {
        "webhook_received": True,
        "last_event_type": last.event_type,
        "last_event_at": last.received_at,
        "last_event_was_test": last.event_type == TEST_EVENT_TYPE,
    }


def send_test_event(merchant, *, by_user=None) -> dict:
    """POST a signed, synthetic event to the merchant's own webhook URL.

    Munim cannot make Razorpay send anything, so this sends the thing Razorpay would
    send, signed the way Razorpay would sign it. Going out over the public internet
    rather than dispatching internally is the entire point: it exercises DNS, TLS,
    the proxy, the route and the stored secret in one shot, which is the list of
    things that are actually broken when a merchant says webhooks do not arrive.
    """
    if not merchant.domain:
        return {"ok": False, "error": "This store has no domain yet, so it has no "
                                      "webhook URL. Finish the catalogue step first."}
    secret = merchant.razorpay_webhook_secret
    if not secret:
        return {"ok": False, "error": "No webhook secret yet. Generate one first."}

    event_id = f"munim_test_{uuid.uuid4().hex[:16]}"
    body = {
        "entity": "event",
        "event": TEST_EVENT_TYPE,
        "contains": [],
        "created_at": int(time.time()),
        "payload": {},
        "notes": {"munim_tenant": merchant.domain, "munim_test": "1"},
    }
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        merchant.webhook_url,
        data=raw,
        headers={"Content-Type": "application/json",
                 "User-Agent": TEST_EVENT_UA,
                 "X-Razorpay-Signature": signature,
                 "X-Razorpay-Event-Id": event_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TEST_EVENT_TIMEOUT_S) as resp:
            status_code = resp.status
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        log.warning("test event to %s failed: %s %s", merchant.domain, exc.code, detail)
        return {"ok": False, "status": exc.code, "event_id": event_id,
                "url": merchant.webhook_url,
                "error": _explain(exc.code, detail)}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "event_id": event_id, "url": merchant.webhook_url,
                "error": f"Could not reach {merchant.webhook_url}: {exc}"}

    LedgerEntry.append(
        tenant=merchant, action="razorpay_webhook_test", decision="ALLOW",
        reason_code="DELIVERED",
        detail={"by_user": getattr(by_user, "email", None), "event_id": event_id,
                "status": status_code},
    )
    return {"ok": bool(payload.get("ok", True)), "status": status_code,
            "event_id": event_id, "url": merchant.webhook_url,
            "message": "Signed test event delivered and its signature verified."}


def _explain(code: int, detail: str) -> str:
    if code == 401:
        return ("The endpoint rejected the signature. The stored webhook secret and the "
                "one this store signs with have diverged -- regenerate and re-copy it.")
    if code == 404:
        return ("The endpoint does not recognise this store's domain. Check that "
                "PUBLIC_HOST and the store domain match the URL in Razorpay.")
    if code == 503:
        return "The endpoint has no webhook secret on file for this store."
    return f"The webhook endpoint answered {code}: {detail}"
