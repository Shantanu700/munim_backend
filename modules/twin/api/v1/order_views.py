"""The orders dashboard's read surface.

In `twin` rather than `recovery` because Order is defined in twin/orders.py --
recovery exists solely for the expiry reaper, and mounting a read API for
another app's model there would split the model from its API for no reason.
twin already carries two URL modules with two mounts (api/v1/urls.py ->
webhooks/, urls_ingest.py -> ingest/), so a third is the established shape.

Kept out of twin/api/v1/views.py, which is already the webhook and the
catalogue.
"""
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
from modules.twin.api.v1.order_serializers import (
    OrderCountsSerializer,
    OrderExportSerializer,
    OrderListResponseSerializer,
    OrderSerializer,
    OrderTilesSerializer,
    order_export_serializer,
    order_row_serializer,
)
from modules.twin.order_analytics import AGENT_VALUES, NEEDS_ATTENTION, UNKNOWN_AGENT
from modules.twin.orders import Order

NO_MERCHANT = "No merchant account is linked to this user."
NO_ORDER = "No such order."

STATUS_ALL = "all"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_VALUES = tuple(Order.Status.values) + (STATUS_ALL, STATUS_NEEDS_ATTENTION)

EXPORT_LIMIT = 5000


class _OrdersAV(BaseAV):
    """Resolve the caller's store, or answer 404.

    Every read here is scoped to that store, and an order belonging to someone
    else answers 404 rather than 403 -- matching the policy and audit surfaces.
    A merchant should not be able to learn that another store's order exists by
    watching status codes.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None

    def _filtered(self, request, tenant, *, apply_status=True):
        """The queryset the list, the counts and the export all share.

        One helper on purpose: "Export these 140 (JSON)" is a promise that the
        export and the list resolve the same rows, and two independently
        written filter blocks would eventually stop agreeing.

        `apply_status=False` is for the counts endpoint, which IS the status
        breakdown and so must see every status while still honouring ?q= and
        ?agent=.

        Returns (queryset, None) or (None, 400 response).
        """
        orders = Order.objects.filter(tenant=tenant).select_related(
            "buyer", "mandate", "step_up")

        agent = request.query_params.get("agent")
        if agent:
            if agent not in AGENT_VALUES:
                return None, Response(
                    {"msg": f"Unknown agent {agent!r}. "
                            f"One of: {', '.join(AGENT_VALUES)}."},
                    status=status.HTTP_400_BAD_REQUEST)
            orders = orders.filter(llm_client="" if agent == UNKNOWN_AGENT else agent)

        if q := request.query_params.get("q"):
            orders = orders.filter(order_analytics.search_q(tenant, q))

        if apply_status and (value := request.query_params.get("status")):
            if value == "awaiting":
                return None, Response(
                    {"msg": "'awaiting' is not an order status -- those rows are "
                            "step-ups awaiting a human. See GET /policy/overview/ "
                            "and /policy/step-up/<token>/."},
                    status=status.HTTP_400_BAD_REQUEST)
            if value not in STATUS_VALUES:
                return None, Response({"msg": f"Unknown status {value!r}."},
                                      status=status.HTTP_400_BAD_REQUEST)
            if value == STATUS_NEEDS_ATTENTION:
                orders = orders.filter(status__in=NEEDS_ATTENTION)
            elif value != STATUS_ALL:
                orders = orders.filter(status=value)

        return orders, None


STATUS_PARAM = OpenApiParameter(
    "status", OpenApiTypes.STR, enum=list(STATUS_VALUES),
    description="One order status, 'all', or 'needs_attention' (failed + expired). "
                "Omit for everything. Note 'awaiting' is NOT valid here -- see "
                "the `source` field on GET /orders/counts.")
AGENT_PARAM = OpenApiParameter(
    "agent", OpenApiTypes.STR, enum=list(AGENT_VALUES),
    description="Which agent placed the order, as the agent described itself. "
                "'unknown' matches orders with no attribution.")
Q_PARAM = OpenApiParameter(
    "q", OpenApiTypes.STR,
    description="Free text over order number, product title and sku, buyer, "
                "Razorpay ids, and reason codes (by name or description).")


class OrderListAV(_OrdersAV):
    """Every order an agent placed, newest first.

    Paginated by cursor rather than page number: the list grows at the head, so
    an order placed between two scrolls would shift every offset and the reader
    would see one row twice and never see another. See base.pagination.

    `total` is returned only on a request that arrived without a cursor -- that
    is the one that needs "Showing 140 of 147", and counting again on every
    scroll tick buys nothing.
    """

    @extend_schema(parameters=[
        STATUS_PARAM, AGENT_PARAM, Q_PARAM,
        OpenApiParameter("cursor", OpenApiTypes.STR,
                         description="`next_cursor` from the previous page. Omit for "
                                     "the first. An unreadable cursor is treated as "
                                     "the first page rather than an error."),
        OpenApiParameter("page_size", OpenApiTypes.INT,
                         description=f"Rows per page, default "
                                     f"{KeysetPagination.page_size}, max "
                                     f"{KeysetPagination.max_page_size}."),
    ])
    @extend_response(OrderListResponseSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        orders, error = self._filtered(request, tenant)
        if error is not None:
            return error

        paginator = KeysetPagination()
        rows, has_more = paginator.paginate(orders, request)
        total = (None if request.query_params.get(paginator.cursor_query_param)
                 else orders.count())
        return paginator.envelope(
            rows, order_row_serializer(rows, many=True).data, has_more, total=total)


class OrderTilesAV(_OrdersAV):
    """The three KPI tiles: paid this month, awaiting approval, average order.

    Ignores every filter, deliberately -- a KPI is a fact about the store, not
    about whichever tab happens to be open. `LedgerExportAV` states its own
    version of this for the same reason.

    "Awaiting approval" counts step-ups waiting on a human, which is a
    different table from orders waiting on a card. The orders list has no such
    status; see the `source` field on GET /orders/counts.
    """

    @extend_response(OrderTilesSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        return order_analytics.tiles(tenant)


class OrderCountsAV(_OrdersAV):
    """The tab bar's numbers.

    Ignores ?status= -- it IS the status breakdown -- but honours ?q= and
    ?agent=, because tab counts that ignored an active search would contradict
    the list sitting underneath them. That asymmetry is the one thing here a
    reader is likely to assume wrong, hence spelling it out.
    """

    @extend_schema(parameters=[AGENT_PARAM, Q_PARAM])
    @extend_response(OrderCountsSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        orders, error = self._filtered(request, tenant, apply_status=False)
        if error is not None:
            return error
        return order_analytics.counts(tenant, queryset=orders)


class OrderExportAV(_OrdersAV):
    """The current filter's rows, as JSON.

    Departs from LedgerExportAV in both the ways that matter, and on purpose.
    That one refuses to filter and refuses to cap because a partial chain
    cannot be re-hashed. Neither holds here: this is a report, not a proof, so
    it honours the filters -- the button says "Export these 140" -- and it
    stops at EXPORT_LIMIT with `truncated` set and the filters echoed back, so
    a merchant who hits the ceiling can see what to narrow.
    """

    @extend_schema(parameters=[STATUS_PARAM, AGENT_PARAM, Q_PARAM])
    @extend_response(OrderExportSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        orders, error = self._filtered(request, tenant)
        if error is not None:
            return error

        rows = list(orders.order_by("-authorized_at", "-id")[:EXPORT_LIMIT + 1])
        truncated = len(rows) > EXPORT_LIMIT
        rows = rows[:EXPORT_LIMIT]
        return {
            "merchant": tenant.domain,
            "exported_at": timezone.now(),
            "filters": {
                "status": request.query_params.get("status"),
                "agent": request.query_params.get("agent"),
                "q": request.query_params.get("q"),
            },
            "count": len(rows),
            "truncated": truncated,
            "orders": order_export_serializer(rows, many=True).data,
        }


class OrderDetailAV(_OrdersAV):
    """One order, in full, with a link to the decision that allowed it.

    Addressed by uuid rather than order_number: order numbers are per-tenant
    and sequential, so they are enumerable, and a 404-on-someone-else's-order
    is worth less when the address itself can be guessed.
    """

    @extend_schema(operation_id="get_order")
    @extend_response(OrderSerializer(component_name="OrderDetail"))
    def get(self, request, order_uuid):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        order = Order.objects.filter(tenant=tenant, uuid=order_uuid).select_related(
            "buyer", "mandate", "step_up").first()
        if order is None:
            return Response({"msg": NO_ORDER}, status=status.HTTP_404_NOT_FOUND)
        return OrderSerializer(order, component_name="OrderDetail").data
