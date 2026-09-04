import hashlib
import hmac
import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.enums import Status
from base.pagination import DefaultPagination
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.core.models import Merchant
from modules.ledger.models import LedgerEntry
from modules.policy.models import Mandate
from modules.policy.reasons import ReasonCode, Verdict
from modules.twin.api.v1.serializers import (
    IngestRequestSerializer,
    IngestResponseSerializer,
    IngestStateSerializer,
    ProductListResponseSerializer,
    ProductSerializer,
    ProductWriteSerializer,
    WebhookHitListResponseSerializer,
    WebhookResponseSerializer,
    product_list_serializer,
    webhook_hit_list_serializer,
)
from modules.twin.choices import IngestJobStatus, WebhookHitOutcome
from modules.twin.models import Event, IngestJob, Product, WebhookHit
from modules.twin.orders import Order
from modules.twin.tasks import run_ingest_job

log = logging.getLogger(__name__)

SPEND_EVENT = "payment_link.paid"

TERMINAL_EVENTS = {
    "payment_link.expired": Order.Status.EXPIRED,
    "payment_link.cancelled": Order.Status.FAILED,
}

ATTEMPT_FAILED_EVENT = "payment.failed"




def _notes(body: dict) -> dict:
    """Razorpay echoes the notes we set at creation on every related event.

    Real payloads send an empty LIST when notes are unset, not an empty object,
    so `or {}` is load-bearing rather than defensive noise.
    """
    for entity in (body.get("payload") or {}).values():
        notes = ((entity or {}).get("entity") or {}).get("notes") or {}
        if isinstance(notes, dict) and notes:
            return notes
    return {}


def _resolve_tenant(body: dict) -> Merchant | None:
    """Tier 0 has no order record of its own, so the tenant travels in notes.

    Only used by the legacy single-secret route; the per-merchant route knows
    the tenant from the URL before it verifies anything.
    """
    if domain := _notes(body).get("munim_tenant"):
        return Merchant.objects.filter(domain=domain).first()
    if Merchant.objects.count() == 1:
        return Merchant.objects.first()
    return None


def _record_spend(tenant, body: dict) -> None:
    """Move the mandate's spent budget once a payment is actually confirmed.

    Not at ALLOW time: an abandoned cart must not eat a buyer's budget. Safe
    against redelivery because Event is idempotent on x-razorpay-event-id, so
    this runs only for an event id we have not already stored.

    ponytail: that is the ONLY thing making the mandate spend idempotent. Two
    distinct event ids naming one payment link would move the budget twice,
    because record_spend below is unconditional. Razorpay does not send a link
    two paid events, so this has never bitten -- but the guard is the event id
    and nothing else, and it is worth knowing that before adding an event type
    to the dispatch table that also means "money moved".
    """
    jti = _notes(body).get("munim_mandate")
    if not jti:
        return
    mandate = Mandate.objects.filter(jti=jti, tenant=tenant).first()
    if mandate is None:
        log.warning("payment for unknown mandate %s on %s", jti, tenant.domain)
        return

    entity = (body.get("payload") or {}).get("payment_link", {}).get("entity", {})
    paid = int(entity.get("amount_paid") or entity.get("amount") or 0)
    if paid <= 0:
        return

    mandate.record_spend(paid)

    plink_id = entity.get("id", "")
    if plink_id:
        Order.objects.filter(tenant=tenant, plink_id=plink_id).exclude(
            status=Order.Status.PAID).update(
            status=Order.Status.PAID, paid_at=timezone.now())
    LedgerEntry.append(
        tenant=tenant, action="payment_confirmed", decision="ALLOW", reason_code="PAID",
        detail={"mandate_jti": str(jti), "amount_paise": paid,
                "plink_id": entity.get("id", ""), "spent_paise": mandate.spent_paise},
    )
    log.info("mandate %s spent now %s paise", jti, mandate.spent_paise)


def _find_order(tenant, body: dict):
    """The order an event belongs to.

    Two routes, because the events do not agree on what they carry. A
    payment_link.* event carries the link entity, so plink_id resolves it. A
    payment.* event carries only the payment entity and no link id at all, so
    the only handle is the reference we planted in the link's notes at
    creation (munim_ref -- see agentmcp/checkout._link_for). Links created
    before that note existed are unresolvable by the second route, which is why
    callers have to tolerate None.
    """
    entity = (body.get("payload") or {}).get("payment_link", {}).get("entity", {})
    if plink_id := entity.get("id", ""):
        if order := Order.objects.filter(tenant=tenant, plink_id=plink_id).first():
            return order
    if reference_id := _notes(body).get("munim_ref"):
        return Order.objects.filter(tenant=tenant, reference_id=reference_id).first()
    return None


def _close_order(tenant, body: dict, new_status: str) -> None:
    """End an order's life, because Razorpay says the link is done.

    Only ever moves a PENDING row. That is what makes it idempotent against
    redelivery, and it is also what stops an `expired` arriving after a `paid`
    from overwriting a sale -- an ordering Razorpay does not promise.
    """
    order = _find_order(tenant, body)
    if order is None:
        log.warning("razorpay %s: no order matched on %s",
                    body.get("event"), tenant.domain)
        return

    if new_status == Order.Status.EXPIRED and order.failed_attempts:
        reason = ReasonCode.PAYMENT_ATTEMPT_FAILED
    elif new_status == Order.Status.EXPIRED:
        reason = ReasonCode.LINK_EXPIRED
    else:
        reason = ReasonCode.LINK_CANCELLED

    moved = Order.objects.filter(pk=order.pk, status=Order.Status.PENDING).update(
        status=new_status, failure_reason_code=reason)
    if not moved:
        log.info("razorpay %s: order %s already %s",
                 body.get("event"), order.order_number, order.status)
        return

    LedgerEntry.append(
        tenant=tenant, action="payment_link_closed", decision=Verdict.DENY,
        reason_code=reason,
        detail={"order_number": order.order_number, "plink_id": order.plink_id,
                "amount_paise": order.amount_paise, "status": new_status},
    )


def _record_failed_attempt(tenant, body: dict) -> None:
    """Count a failed payment attempt. Deliberately does NOT change status.

    A card that was declined is not an order that failed: the link stays
    payable until expire_by, and Razorpay's own docs describe buyers retrying.
    What the count is for is letting the reaper tell "someone tried and could
    not pay" from "nobody tried at all" -- the two halves of Needs attention,
    which want different things from the merchant.
    """
    order = _find_order(tenant, body)
    if order is None:
        log.info("razorpay payment.failed: no order matched on %s", tenant.domain)
        return

    Order.objects.filter(pk=order.pk).update(
        failed_attempts=F("failed_attempts") + 1, last_failure_at=timezone.now())

    LedgerEntry.append(
        tenant=tenant, action="payment_attempt_failed", decision=Verdict.DENY,
        reason_code=ReasonCode.PAYMENT_ATTEMPT_FAILED,
        detail={"order_number": order.order_number, "plink_id": order.plink_id,
                "amount_paise": order.amount_paise,
                "failed_attempts": order.failed_attempts + 1},
    )


def _hostname(value: str) -> str:
    """A bare host from anything host-shaped: a full URL, or a host on its own.

    Merchant.domain is a path segment in both webhook_url and mcp_url, so a value
    that still carries "https://" produces addresses no one can reach -- and worse,
    asgi.py's /mcp/<domain> route is a single-segment match, so the embedded slashes
    make that merchant's MCP door unroutable rather than merely ugly.
    """
    parsed = urlparse(value if "//" in value else f"//{value}")
    return (parsed.netloc or value).lower().strip("/")


def _extract_business_name(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    hostname = hostname.removeprefix("www.")
    name = hostname.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def _record_hit(response, *, outcome, tenant=None, merchant_domain="", event_id="",
                 event_type="", signature_valid=None, error_detail=""):
    """Log one row per POST to the webhook receiver, then hand back its response
    unchanged.

    Never allowed to turn a working response into a 500: a failure to write the
    hit log must not make Razorpay retry (or eventually disable) an endpoint
    that actually handled the request correctly.
    """
    try:
        WebhookHit.objects.create(
            tenant=tenant, merchant_domain=merchant_domain, outcome=outcome,
            razorpay_event_id=event_id, event_type=event_type,
            signature_valid=signature_valid, error_detail=error_detail,
        )
    except Exception:
        log.exception("failed to record webhook hit (outcome=%s)", outcome)
    return response


class RazorpayWebhookAV(BaseAV):
    """Receive a Razorpay webhook.

    /webhooks/razorpay/<merchant> is the real route: the tenant comes from the
    path, so we know WHOSE secret to verify with before trusting anything. A
    single global secret cannot work for two merchants, because you would have
    to decide who sent a payload in order to check whether it is genuine.

    /webhooks/razorpay (no merchant) is deprecated and kept only so an already
    configured dashboard URL does not silently stop working mid-migration.
    """

    authentication = False

    @extend_response(WebhookResponseSerializer())
    def post(self, request, merchant: str | None = None):
        raw = request.body
        event_id = request.headers.get("x-razorpay-event-id", "")

        tenant = None
        if merchant:
            tenant = Merchant.objects.filter(domain=merchant).first()
            if tenant is None:
                log.warning("webhook for unknown merchant %r", merchant)
                return _record_hit(
                    Response({"ok": False, "error": "unknown merchant"},
                            status=status.HTTP_404_NOT_FOUND),
                    outcome=WebhookHitOutcome.UNKNOWN_MERCHANT, merchant_domain=merchant,
                    event_id=event_id, error_detail="unknown merchant")
            secret = (tenant.razorpay_webhook_secret or "").encode()
            if not secret:
                log.error("merchant %s has no webhook secret configured", merchant)
                return _record_hit(
                    Response({"ok": False, "error": "merchant not configured"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE),
                    outcome=WebhookHitOutcome.MERCHANT_NOT_CONFIGURED, tenant=tenant,
                    merchant_domain=merchant, event_id=event_id,
                    error_detail="no webhook secret configured")
        else:
            secret = getattr(settings, "RZP_WEBHOOK_SECRET", b"") or b""
            if not secret:
                log.error("legacy webhook route called with no RZP_WEBHOOK_SECRET set")
                return _record_hit(
                    Response({"ok": False, "error": "not configured"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE),
                    outcome=WebhookHitOutcome.LEGACY_NOT_CONFIGURED, event_id=event_id,
                    error_detail="RZP_WEBHOOK_SECRET not set")

        expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-razorpay-signature", "")):
            log.warning("razorpay webhook: bad signature, event_id=%s",
                        request.headers.get("x-razorpay-event-id"))
            return _record_hit(
                Response({"ok": False, "error": "invalid signature"},
                        status=status.HTTP_401_UNAUTHORIZED),
                outcome=WebhookHitOutcome.INVALID_SIGNATURE, tenant=tenant,
                merchant_domain=merchant or "", event_id=event_id,
                signature_valid=False, error_detail="invalid signature")

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return _record_hit(
                Response({"ok": False, "error": "malformed json"},
                        status=status.HTTP_400_BAD_REQUEST),
                outcome=WebhookHitOutcome.MALFORMED_JSON, tenant=tenant,
                merchant_domain=merchant or "", event_id=event_id,
                signature_valid=True, error_detail="malformed json")

        if tenant is None:
            tenant = _resolve_tenant(body)
        if tenant is None:
            log.error("razorpay webhook: no tenant for event %s", body.get("event"))
            return _record_hit(
                Response({"ok": False, "error": "unknown tenant"}, status=status.HTTP_200_OK),
                outcome=WebhookHitOutcome.UNKNOWN_TENANT, merchant_domain=merchant or "",
                event_id=event_id, event_type=body.get("event", ""),
                signature_valid=True, error_detail="unknown tenant")

        event, created = Event.objects.get_or_create(
            razorpay_event_id=event_id or None,
            defaults={
                "tenant": tenant,
                "event_type": body.get("event", ""),
                "payload": body,
                "signature_valid": True,
            },
        )
        if created:
            if event.event_type == SPEND_EVENT:
                _record_spend(tenant, body)
            elif new_status := TERMINAL_EVENTS.get(event.event_type):
                _close_order(tenant, body, new_status)
            elif event.event_type == ATTEMPT_FAILED_EVENT:
                _record_failed_attempt(tenant, body)

        log.info("razorpay %s %s created=%s", event.event_type, event_id, created)
        return _record_hit(
            Response({"ok": True, "duplicate": not created, "event_pk": event.pk},
                    status=status.HTTP_200_OK),
            outcome=WebhookHitOutcome.PROCESSED if created else WebhookHitOutcome.DUPLICATE,
            tenant=tenant, merchant_domain=merchant or "", event_id=event_id,
            event_type=event.event_type, signature_valid=True)


class WebhookHitListAV(BaseAV):
    """The caller's Razorpay webhook hit log, most recent first."""

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    @extend_schema(parameters=[
        OpenApiParameter("page", OpenApiTypes.INT, description="1-based page number."),
        OpenApiParameter("page_size", OpenApiTypes.INT,
                         description=f"Rows per page, default {DefaultPagination.page_size}, "
                                     f"max {DefaultPagination.max_page_size}."),
    ])
    @extend_response(WebhookHitListResponseSerializer())
    def get(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return Response({"msg": "No merchant account is linked to this user."},
                            status=status.HTTP_404_NOT_FOUND)

        hits = WebhookHit.objects.filter(tenant=tenant).order_by("-received_at")
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(hits, request, view=self)
        return paginator.get_paginated_response(webhook_hit_list_serializer(page, many=True).data)


class IngestAV(BaseAV):
    """Start an asynchronous catalog ingestion for the caller's merchant."""

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    @extend_response(IngestStateSerializer())
    def get(self, request):
        """Everything the catalogue screen needs on load, in one request.

        Deliberately not a bare product list: a client that has just reloaded also
        needs to know whether an ingestion is still running, so it can re-attach its
        EventSource instead of showing a stale, half-filled table with no explanation.
        """
        tenant = request.user.get_merchant()
        if tenant is None:
            return Response({"msg": "No merchant account is linked to this user."},
                            status=status.HTTP_404_NOT_FOUND)

        job = IngestJob.objects.filter(
            tenant=tenant, job_status__in=[IngestJobStatus.PENDING, IngestJobStatus.RUNNING],
        ).first()

        return {
            "job": job.uuid if job else None,
            "stream_url": f"/ingest/{job.uuid}/stream" if job else None,
            "status": job.get_job_status_display().lower() if job else None,
            "domain": tenant.domain,
            "business_name": tenant.business_name,
            "webhook_url": tenant.webhook_url,
            "mcp_url": tenant.mcp_url,
        }

    @extend_schema(
        request=IngestRequestSerializer(),
    )
    @extend_response(IngestResponseSerializer())
    def post(self, request):
        body = IngestRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        tenant = request.user.get_merchant()
        if tenant is None:
            return Response({"msg": "No merchant account is linked to this user."},
                            status=status.HTTP_404_NOT_FOUND)

        if not tenant.domain:
            domain = _hostname(data.get("domain") or data["source"])
            tenant.domain = domain
            if not tenant.business_name:
                tenant.business_name = data.get("business_name") or _extract_business_name(data["source"])
            try:
                tenant.save(update_fields=["domain", "business_name"])
            except IntegrityError:
                return Response({"msg": f"Domain {domain!r} is already in use by another merchant."},
                                status=status.HTTP_400_BAD_REQUEST)
        elif data.get("business_name"):
            tenant.business_name = data["business_name"]
            tenant.save(update_fields=["business_name"])

        job = IngestJob.objects.filter(
            tenant=tenant, job_status__in=[IngestJobStatus.PENDING, IngestJobStatus.RUNNING],
        ).first()
        if job is None:
            job = IngestJob.objects.create(
                tenant=tenant, created_by=request.user,
                source=data["source"], match=data.get("match", ""), limit=data.get("limit", 0),
            )
            run_ingest_job.delay(job.id)

        return {
            "job": job.uuid,
            "stream_url": f"/ingest/{job.uuid}/stream",
            "status": job.get_job_status_display().lower(),
            "webhook_url": tenant.webhook_url,
            "mcp_url": tenant.mcp_url,
        }


class ProductListAV(BaseAV):
    """The caller's catalog, a page at a time, and manual additions to it.

    Paginated rather than served whole with the ingest state: a catalogue is
    unbounded, and a merchant reloading the screen should not pay for every row
    they own.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    @extend_schema(parameters=[
        OpenApiParameter("page", OpenApiTypes.INT, description="1-based page number."),
        OpenApiParameter("page_size", OpenApiTypes.INT,
                         description=f"Rows per page, default {DefaultPagination.page_size}, "
                                     f"max {DefaultPagination.max_page_size}."),
    ])
    @extend_response(ProductListResponseSerializer())
    def get(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return Response({"msg": "No merchant account is linked to this user."},
                            status=status.HTTP_404_NOT_FOUND)

        products = Product.objects.filter(tenant=tenant).order_by("-fetched_at")
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(products, request, view=self)
        return paginator.get_paginated_response(product_list_serializer(page, many=True).data)

    @extend_schema(request=ProductWriteSerializer())
    @extend_response(product_list_serializer())
    def post(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return Response({"msg": "No merchant account is linked to this user."},
                            status=status.HTTP_404_NOT_FOUND)

        body = ProductWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                product = Product.objects.create(
                    tenant=tenant, source="manual", confidence=1.0, **body.validated_data,
                )
        except IntegrityError:
            return Response({"msg": f"A product with url {body.validated_data['url']!r} already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        return product_list_serializer(product).data


class ProductDetailAV(BaseAV):
    """Read, edit or remove one product in the caller's own catalog."""

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _get_product(self, request, product_uuid):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": "No merchant account is linked to this user."},
                                  status=status.HTTP_404_NOT_FOUND)
        product = Product.objects.filter(tenant=tenant, uuid=product_uuid).first()
        if product is None:
            return None, Response({"msg": "No such product."}, status=status.HTTP_404_NOT_FOUND)
        return product, None

    @extend_schema(operation_id="get_ingest_product")
    @extend_response(ProductSerializer(component_name="ProductDetail"))
    def get(self, request, product_uuid):
        """Everything about one product -- the columns the list omits so that a
        catalogue page stays cheap."""
        product, error = self._get_product(request, product_uuid)
        if error is not None:
            return error

        return ProductSerializer(product).data

    @extend_schema(request=ProductWriteSerializer(partial=True))
    @extend_response(product_list_serializer())
    def put(self, request, product_uuid):
        """Partial on purpose: flipping is_visible_to_agents must not require the
        client to echo back every other column to avoid clobbering it."""
        product, error = self._get_product(request, product_uuid)
        if error is not None:
            return error

        body = ProductWriteSerializer(product, data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                body.save()
        except IntegrityError:
            return Response({"msg": f"A product with url {body.validated_data.get('url')!r} already exists."},
                            status=status.HTTP_400_BAD_REQUEST)
        return product_list_serializer(product).data

    def delete(self, request, product_uuid):
        product, error = self._get_product(request, product_uuid)
        if error is not None:
            return error

        product.status = Status.DELETED
        product.save(update_fields=["status"])
        return Response({"msg": "Deleted."}, status=status.HTTP_200_OK)
