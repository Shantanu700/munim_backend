from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.pagination import DefaultPagination
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.core.models import Merchant
from modules.ledger.api.v1.serializers import (
    AuditVerifyResponseSerializer,
    LedgerEntryDetailSerializer,
    LedgerEntrySerializer,
    LedgerExportSerializer,
    LedgerListResponseSerializer,
)
from modules.ledger.models import LedgerEntry
from modules.policy.reasons import Verdict, describe


NO_MERCHANT = "No merchant account is linked to this user."
NO_ENTRY = "No such ledger entry."


def _row(entry: LedgerEntry) -> dict:
    """One entry flattened for the audit table.

    The agent, buyer and amount are read out of `detail` here rather than in
    the serializer, so the screen never has to know the shape of that column --
    and `.get` throughout because entries are permanent: rows written before a
    key existed are still rendered, just with that field blank.
    """
    detail = entry.detail or {}
    return {
        "seq": entry.seq,
        "at": entry.created_at,
        "action": entry.action,
        "decision": entry.decision,
        "reason_code": entry.reason_code,
        "reason_description": describe(entry.reason_code),
        "amount_paise": detail.get("amount_paise"),
        "agent": detail.get("agent") or "",
        "buyer_uuid": detail.get("buyer_uuid") or "",
        "buyer_label": detail.get("buyer_label") or "",
        "chain_hash": entry.chain_hash,
    }


class _LedgerAV(BaseAV):
    """Resolve the caller's store, or answer 404.

    Every read here is scoped to that store. One merchant must not be able to
    address another's chain by guessing a sequence number.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None


class AuditVerifyAV(BaseAV):
    "Public proof endpoint. Verifies the hash chain for one or every tenant."

    authentication = {"get": False}


    @extend_schema(parameters=[
        OpenApiParameter(
            name="merchant",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description="Optional merchant domain to verify. If not provided, verifies all tenants.",
        )
    ])
    @extend_response(AuditVerifyResponseSerializer())
    def get(self, request):
        domain = request.query_params.get("merchant")
        tenants = Merchant.objects.filter(domain=domain) if domain else Merchant.objects.all()
        results = {t.domain: LedgerEntry.verify(t) for t in tenants}
        ok = all(r["ok"] for r in results.values())
        return Response(
            {"ok": ok, "merchants": results},
            status=status.HTTP_200_OK if ok else status.HTTP_409_CONFLICT,
        )


class LedgerListAV(_LedgerAV):
    """The audit table: every decision this store made, newest first.

    Paginated on `seq` descending rather than on a timestamp. `seq` is unique
    per tenant and gapless, so a page boundary cannot duplicate or skip a row
    the way two entries sharing a timestamp can -- and it is the same order the
    chain itself is built in.
    """

    @extend_schema(parameters=[
        OpenApiParameter("decision", OpenApiTypes.STR,
                         description="Filter to one verdict: "
                                     f"{', '.join(Verdict.values)}. Omit for everything.",
                         enum=Verdict.values),
        OpenApiParameter("page", OpenApiTypes.INT, description="1-based page number."),
        OpenApiParameter("page_size", OpenApiTypes.INT,
                         description=f"Rows per page, default {DefaultPagination.page_size}, "
                                     f"max {DefaultPagination.max_page_size}."),
    ])
    @extend_response(LedgerListResponseSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        entries = LedgerEntry.objects.filter(tenant=tenant).order_by("-seq")
        decision = request.query_params.get("decision")
        if decision:
            if decision not in Verdict.values:
                return Response({"msg": f"Unknown decision {decision!r}."},
                                status=status.HTTP_400_BAD_REQUEST)
            entries = entries.filter(decision=decision)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(entries, request, view=self)
        rows = [_row(entry) for entry in page]
        return paginator.get_paginated_response(
            LedgerEntrySerializer(rows, many=True).data)


class LedgerEntryAV(_LedgerAV):
    """One entry, with the hash it carries and the one it commits to.

    Addressed by `seq` -- the number the table already shows, and the number a
    broken verify names.
    """

    @extend_schema(operation_id="get_audit_entry")
    @extend_response(LedgerEntryDetailSerializer())
    def get(self, request, seq):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        entry = LedgerEntry.objects.filter(tenant=tenant, seq=seq).first()
        if entry is None:
            return Response({"msg": NO_ENTRY}, status=status.HTTP_404_NOT_FOUND)

        return LedgerEntryDetailSerializer({
            **_row(entry),
            "prev_hash": entry.prev_hash,
            "detail": entry.detail or {},
        }).data


class LedgerExportAV(_LedgerAV):
    """The whole chain, in the form someone else can re-hash.

    Not paginated and not filtered, deliberately: a partial chain cannot be
    verified. Every entry from genesis is needed to recompute the hashes, so
    this returns them all, in chain order, as the columns that were hashed.
    """

    @extend_response(LedgerExportSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        entries = LedgerEntry.objects.filter(tenant=tenant).order_by("seq")
        return {
            "merchant": tenant.domain,
            "exported_at": timezone.now(),
            "verification": LedgerEntry.verify(tenant),
            "entries": [{
                "seq": entry.seq,
                "at": entry.created_at,
                "action": entry.action,
                "decision": entry.decision,
                "reason_code": entry.reason_code,
                "detail": entry.detail,
                "prev_hash": entry.prev_hash,
                "chain_hash": entry.chain_hash,
            } for entry in entries.iterator()],
        }
