"""The buyer's own order history.

Mounted at /buyer/ next to /buyer/mandates/, not under /orders/. /orders/ is the
merchant's screen: every view there carries IsMerchantPermission and answers a
buyer's login with a 403. One prefix holding two opposite authorization models
is a prefix where the next reader has to check each view to know which one they
are looking at -- the same argument policy/api/v1/buyer_urls.py makes for
keeping mandates out of /policy/.

Kept out of order_views.py for the same reason and one more: that module's
queryset is scoped by tenant and reachable by any merchant login, and a buyer
view accidentally inheriting from it would be scoped to the wrong thing in the
most expensive possible direction.

404 and never 403 for another buyer's order, matching the mandate surface: which
status code comes back must not be a way to learn what exists.
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.pagination import KeysetPagination
from base.views import BaseAV
from modules.core.models import Buyer
from modules.twin import order_analytics
from modules.twin.api.v1.buyer_order_serializers import (
    BuyerOrderDetailSerializer,
    BuyerOrderListResponseSerializer,
    BuyerOrderSerializer,
)
from modules.twin.orders import Order

NO_ORDER = "No such order."

STATUS_PARAM = OpenApiParameter(
    "status", OpenApiTypes.STR, enum=list(Order.Status.values),
    description="One order status. Omit for every status.")
Q_PARAM = OpenApiParameter(
    "q", OpenApiTypes.STR,
    description="Free text over order number, product title, sku and url, the "
                "merchant's domain, and failure reasons -- by code name or by "
                "the sentence shown for it.")
MERCHANT_PARAM = OpenApiParameter(
    "merchant", OpenApiTypes.STR,
    description="Exact merchant domain, narrowing this cross-store list to one "
                "shop. A domain the buyer has never ordered from matches "
                "nothing rather than erroring -- see the view.")
CURSOR_PARAM = OpenApiParameter(
    "cursor", OpenApiTypes.STR,
    description="`next_cursor` from the previous page. Omit for the first. An "
                "unreadable cursor is treated as the first page, not an error.")
PAGE_SIZE_PARAM = OpenApiParameter(
    "page_size", OpenApiTypes.INT,
    description=f"Rows per page, default {KeysetPagination.page_size}, max "
                f"{KeysetPagination.max_page_size}.")


class _BuyerOrderAV(BaseAV):
    """Resolve the caller's Buyer row and scope every query to it."""

    authentication = True

    def _buyer_or_none(self, request):
        return Buyer.unfiltered_objects.filter(user=request.user).first()

    def _mine(self, request):
        buyer = self._buyer_or_none(request)
        if buyer is None:
            return Order.objects.none()
        return (Order.objects.filter(buyer=buyer)
                .select_related("tenant")
                .prefetch_related("payments"))

    def _order(self, request, order_uuid):
        row = self._mine(request).filter(uuid=order_uuid).first()
        if row is None:
            return None, Response({"msg": NO_ORDER},
                                  status=status.HTTP_404_NOT_FOUND)
        return row, None


class BuyerOrderListAV(_BuyerOrderAV):
    """Every order this buyer has placed, newest first.

    Across all merchants, unlike the MCP tools, which are pinned to the store
    whose endpoint the agent is talking to. A logged-in buyer asking for their
    own orders is not scoped to one shop.

    Paginated by cursor and not by page number, for the reason OrderListAV
    states: the list grows at the head, so an order placed between two scrolls
    shifts every offset, and the reader sees one row twice and never sees
    another. The ordering KeysetPagination positions on -- (-authorized_at,
    -id) -- is this model's Meta ordering and is covered here by
    idx_order_buyer_authz, so the buyer's page costs the same index walk the
    merchant's does.

    `total` comes back only on a request that arrived without a cursor: that is
    the one that needs "Showing 20 of 147", and counting again on every scroll
    tick buys nothing.
    """

    def _filtered(self, request):
        """This buyer's orders, narrowed by the query string.

        Returns (queryset, None) or (None, 400 response). Unknown ?status= is a
        400 because it is a closed set and a typo there silently returns an
        empty history, which reads as data loss. ?merchant= is free text with no
        such set to check against -- and the honest answer to "orders from a
        shop you have not used" is no orders, not an error.
        """
        rows = self._mine(request)

        wanted = (request.query_params.get("status") or "").strip().lower()
        if wanted:
            if wanted not in {s.value for s in Order.Status}:
                return None, Response(
                    {"msg": f"Unknown status {wanted!r}."},
                    status=status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(status=wanted)

        if merchant := (request.query_params.get("merchant") or "").strip():
            rows = rows.filter(tenant__domain__iexact=merchant)

        if q := (request.query_params.get("q") or "").strip():
            rows = rows.filter(order_analytics.buyer_search_q(q))

        return rows, None

    @extend_schema(parameters=[
        STATUS_PARAM, Q_PARAM, MERCHANT_PARAM, CURSOR_PARAM, PAGE_SIZE_PARAM,
    ])
    @extend_response(BuyerOrderListResponseSerializer())
    def get(self, request):
        rows, error = self._filtered(request)
        if error is not None:
            return error

        paginator = KeysetPagination()
        page, has_more = paginator.paginate(rows, request)
        total = (None if request.query_params.get(paginator.cursor_query_param)
                 else rows.count())
        return paginator.envelope(
            page, BuyerOrderSerializer(page, many=True).data, has_more, total=total)


class BuyerOrderDetailAV(_BuyerOrderAV):
    """One order, with every payment attempt made against it."""

    @extend_schema(operation_id="get_buyer_order")
    @extend_response(BuyerOrderDetailSerializer())
    def get(self, request, order_uuid):
        order, error = self._order(request, order_uuid)
        if error is not None:
            return error
        return BuyerOrderDetailSerializer(order).data
