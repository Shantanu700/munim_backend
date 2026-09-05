"""The payments screen's read surface, for the merchant.

The one audience that could not see payment attempts. The buyer gets them nested
on their own order; /dashboard/payments is an aggregate card, not rows; and
/orders/ shows only what reproject() promoted onto the order -- the latest
MEANINGFUL attempt, which by construction hides the two that failed before it.
"Why did MNM-104 take three tries" and "which links are still payable right now"
had no endpoint at all.

Kept out of order_views.py, which is Order's surface, and out of
twin/api/v1/views.py, which is already the webhook and the catalogue. twin
carries a url module per mount and this is the fifth; order_views.py:6 calls
that the established shape.

404 and never 403 for another store's payment, matching the orders, policy and
audit surfaces: which status code comes back must not be a way to learn what
exists.
"""
from uuid import UUID

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.pagination import KeysetPagination
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.twin import order_analytics
from modules.twin.api.v1.payment_serializers import (
    PaymentDetailSerializer,
    PaymentListResponseSerializer,
    PaymentRowSerializer,
)
from modules.twin.payments import Payment

NO_MERCHANT = "No merchant account is linked to this user."
NO_PAYMENT = "No such payment."

STATUS_VALUES = tuple(Payment.Status.values)

LIVE_VALUES = {"true": True, "false": False}


class PaymentKeysetPagination(KeysetPagination):
    """The cursor, positioned on Payment's own time column.

    requested_at rather than created_at, which ModelBase declares auto_now and
    which therefore moves every time a webhook touches the row -- a cursor on it
    would reshuffle the list underneath a scroll. Payment's docstring makes the
    same point about why the column exists at all.
    """

    timestamp_field = "requested_at"


class _PaymentsAV(BaseAV):
    """Resolve the caller's store, and scope every read to it.

    Payment inherits TenantScoped, so this filters on the payment's own tenant
    column rather than joining through the order -- the two cannot disagree,
    create_payment copies it off the locked order row.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None

    def _filtered(self, request, tenant):
        """This store's payment attempts, narrowed by the query string.

        Returns (queryset, None) or (None, 400 response).

        The select_related is load-bearing, not a nicety: every row renders
        order_number, order_title and buyer_label, so without it an
        infinite-scrolling list issues three queries per row. That is the N+1
        tests/test_order_schema.py's query-budget half exists to catch on the
        orders list, and it grows with the merchant's success.
        """
        rows = Payment.objects.filter(tenant=tenant).select_related(
            "order", "order__buyer", "order__mandate")

        wanted = (request.query_params.get("status") or "").strip().lower()
        if wanted:
            if wanted not in STATUS_VALUES:
                return None, Response(
                    {"msg": f"Unknown status {wanted!r}. "
                            f"One of: {', '.join(STATUS_VALUES)}."},
                    status=status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(status=wanted)

        if raw := (request.query_params.get("order") or "").strip():
            try:
                order_uuid = UUID(raw)
            except ValueError:
                return None, Response(
                    {"msg": f"{raw!r} is not a valid order uuid."},
                    status=status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(order__uuid=order_uuid)

        if (raw := (request.query_params.get("live") or "").strip().lower()):
            if raw not in LIVE_VALUES:
                return None, Response(
                    {"msg": f"Unknown live {raw!r}. One of: true, false."},
                    status=status.HTTP_400_BAD_REQUEST)
            now = timezone.now()
            live = {"status": Payment.Status.PENDING, "expires_at__gt": now}
            rows = rows.filter(**live) if LIVE_VALUES[raw] else rows.exclude(**live)

        if q := (request.query_params.get("q") or "").strip():
            rows = rows.filter(order_analytics.payment_search_q(q))

        return rows, None


STATUS_PARAM = OpenApiParameter(
    "status", OpenApiTypes.STR, enum=list(STATUS_VALUES),
    description="One payment status. Omit for every status. Note a link that "
                "has lapsed still reads 'pending' until the reaper closes it -- "
                "use ?live= for what is actually payable.")
ORDER_PARAM = OpenApiParameter(
    "order", OpenApiTypes.UUID,
    description="An order's uuid, narrowing to every attempt made against it -- "
                "the 'why did this take three tries' view. An order belonging "
                "to another store matches nothing rather than erroring.")
LIVE_PARAM = OpenApiParameter(
    "live", OpenApiTypes.BOOL,
    description="true for links that are still payable (pending AND unexpired), "
                "false for everything else. Not the same as status=pending.")
Q_PARAM = OpenApiParameter(
    "q", OpenApiTypes.STR,
    description="Free text over order number, product title and sku, buyer, the "
                "Razorpay link and reference ids on THIS attempt, and failure "
                "reasons by code name or by the sentence shown for them.")
CURSOR_PARAM = OpenApiParameter(
    "cursor", OpenApiTypes.STR,
    description="`next_cursor` from the previous page. Omit for the first. An "
                "unreadable cursor is treated as the first page, not an error.")
PAGE_SIZE_PARAM = OpenApiParameter(
    "page_size", OpenApiTypes.INT,
    description=f"Rows per page, default {PaymentKeysetPagination.page_size}, max "
                f"{PaymentKeysetPagination.max_page_size}.")


class PaymentListAV(_PaymentsAV):
    """Every payment link this store has minted, newest first.

    Across orders, and one row per ATTEMPT -- which is the difference from
    /orders/, where three tries collapse into one row showing whichever attempt
    reproject() promoted.

    Paginated by cursor rather than page number for the reason OrderListAV
    states: the list grows at the head, so a retry minted between two scrolls
    shifts every offset and the reader sees one row twice and never sees
    another. The (-requested_at, -id) walk is covered by
    idx_payment_tenant_req_id.

    `total` comes back only on a request that arrived without a cursor -- that
    is the one that needs "Showing 20 of 147", and counting again on every
    scroll tick buys nothing.
    """

    @extend_schema(parameters=[
        STATUS_PARAM, ORDER_PARAM, LIVE_PARAM, Q_PARAM,
        CURSOR_PARAM, PAGE_SIZE_PARAM,
    ])
    @extend_response(PaymentListResponseSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        rows, error = self._filtered(request, tenant)
        if error is not None:
            return error

        paginator = PaymentKeysetPagination()
        page, has_more = paginator.paginate(rows, request)
        total = (None if request.query_params.get(paginator.cursor_query_param)
                 else rows.count())
        return paginator.envelope(
            page, PaymentRowSerializer(page, many=True).data, has_more, total=total)


class PaymentDetailAV(_PaymentsAV):
    """One attempt, with a link to the decision that authorised it.

    Addressed by uuid rather than (order_number, attempt): order numbers are
    per-tenant and sequential, so that pair is enumerable, and a 404-on-someone-
    else's-row is worth less when the address itself can be guessed.
    """

    @extend_schema(operation_id="get_payment")
    @extend_response(PaymentDetailSerializer())
    def get(self, request, payment_uuid):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        payment = Payment.objects.filter(
            tenant=tenant, uuid=payment_uuid).select_related(
            "order", "order__buyer", "order__mandate").first()
        if payment is None:
            return Response({"msg": NO_PAYMENT}, status=status.HTTP_404_NOT_FOUND)
        return PaymentDetailSerializer(payment).data
