"""The buyer's mandate surface: issue, list, read, revoke.

Session-authenticated, like /core/api-keys/, and deliberately NOT reachable with
a platform API key. A key is the thing an AGENT holds; a mandate is the authority
that constrains that agent. A door where the constrained party can widen its own
constraint is the property jws.py exists to prevent, and it would not help to
check the signature at checkout if the holder could mint a new one.

404 and never 403 for another buyer's mandate, matching _MerchantAV and the
api-keys endpoint: which status code comes back must not be a way to learn what
exists.
"""
from django.core.exceptions import ImproperlyConfigured
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.views import BaseAV
from modules.core.models import Buyer, Merchant
from modules.policy import services
from modules.policy.api.v1.mandate_serializers import (
    MandateIssuedSerializer,
    MandateSerializer,
    MandateWriteSerializer,
)
from modules.policy.models import Mandate


NO_MANDATE = "No such mandate."
NO_STORE = "No such store. Check the domain -- it is the one in the store's MCP URL."
NO_SIGNING_KEY = ("Mandate signing is not configured on this deployment, so "
                  "nothing can be issued. Contact support.")


def _store_warnings(store) -> list[dict]:
    warnings = []
    if not store.agent_traffic_enabled:
        warnings.append({
            "code": "STORE_AGENTS_DISABLED",
            "message": "This store has agent purchases switched off right now. "
                       "The mandate is valid and will work as soon as they "
                       "switch them back on.",
        })
    if not store.is_payment_ready:
        warnings.append({
            "code": "MERCHANT_NOT_PAYMENT_READY",
            "message": "This store has not finished connecting a payment "
                       "account, so nothing can be charged there yet.",
        })
    return warnings


class _BuyerAV(BaseAV):
    """Resolve the caller's Buyer row and scope every query to it."""

    authentication = True

    def _buyer_or_none(self, request):
        return Buyer.unfiltered_objects.filter(user=request.user).first()

    def _mine(self, request):
        buyer = self._buyer_or_none(request)
        if buyer is None:
            return Mandate.objects.none()
        return Mandate.objects.filter(buyer=buyer).select_related("tenant")

    def _mandate(self, request, mandate_uuid):
        row = self._mine(request).filter(uuid=mandate_uuid).first()
        if row is None:
            return None, Response({"msg": NO_MANDATE},
                                  status=status.HTTP_404_NOT_FOUND)
        return row, None


class BuyerMandateListCreateAV(_BuyerAV):
    """The caller's mandates, and new ones."""

    @extend_response(MandateSerializer(many=True))
    def get(self, request):
        rows = self._mine(request).order_by("-not_before", "-id")
        return MandateSerializer(rows, many=True).data

    @extend_schema(request=MandateWriteSerializer())
    @extend_response(MandateIssuedSerializer())
    def post(self, request):
        body = MandateWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        store = Merchant.objects.filter(domain=data["domain"]).first()
        if store is None:
            return Response({"msg": NO_STORE}, status=status.HTTP_404_NOT_FOUND)

        buyer = Buyer.for_user(request.user)

        try:
            mandate = services.issue_mandate(
                store,
                subject=data["subject"],
                per_txn_cap_paise=data["per_txn_cap_paise"],
                total_cap_paise=data["total_cap_paise"],
                step_up_ceiling_paise=data["step_up_ceiling_paise"],
                allowed_categories=data["allowed_categories"],
                ttl_hours=data["ttl_hours"],
                buyer=buyer,
                by_user=request.user,
                via="api",
            )
        except ValueError as exc:
            return Response({"msg": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ImproperlyConfigured:
            return Response({"msg": NO_SIGNING_KEY},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return MandateIssuedSerializer(
            mandate, context={"warnings": _store_warnings(store)}).data


class BuyerMandateDetailAV(_BuyerAV):
    @extend_schema(operation_id="get_buyer_mandate")
    @extend_response(MandateIssuedSerializer(component_name="MandateDetail"))
    def get(self, request, mandate_uuid):
        mandate, error = self._mandate(request, mandate_uuid)
        if error is not None:
            return error
        return MandateIssuedSerializer(mandate).data


class BuyerMandateRevokeAV(_BuyerAV):
    @extend_schema(request=None)
    @extend_response(MandateSerializer(component_name="MandateRevoked"))
    def post(self, request, mandate_uuid):
        mandate, error = self._mandate(request, mandate_uuid)
        if error is not None:
            return error

        services.revoke_mandate(mandate, by_user=request.user)
        mandate.refresh_from_db()
        return Response(MandateSerializer(mandate).data, status=status.HTTP_200_OK)
