"""The merchant's home dashboard.

In `twin` for the reason order_views.py gives: Order and Product are defined
here, and this screen is mostly about them. A fourth URL module rather than
another block in api/v1/views.py, which is already the webhook and the
catalogue -- twin/urls_orders.py established the shape.

The store-policy panel is assembled by policy.analytics.store_card and merely
passed through here -- the decoding of PolicyRule.config belongs to the app that
owns those semantics.

Nothing in this module duplicates a surface that already exists. The policy
panel's "open" arrow goes to GET /policy/overview/, which returns every policy,
every rule and the policy tiles; the orders card's goes to GET /orders/, and
each per-agent bar to GET /orders/?agent=<key> -- the agent keys returned here
are chosen to be valid values of that filter, which is what makes a bar
clickable. /dashboard/policy exists only so a client can refresh that one panel
without re-fetching the screen.
"""
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.pagination import DefaultPagination
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.policy import analytics as policy_analytics
from modules.policy.registry import PRODUCT_DATA_FIELDS
from modules.twin import dashboard_analytics as dash
from modules.twin.api.v1.dashboard_serializers import (
    DAYS_HELP,
    CatalogReadsCardSerializer,
    DashboardSerializer,
    PaymentsCardSerializer,
    PolicyCardSerializer,
    ProductRowsSerializer,
    ProductsCardSerializer,
    RevenueCardSerializer,
)
from modules.twin.api.v1.serializers import product_list_serializer
from modules.twin.dashboard_analytics import (
    ALL_PRESENT,
    MAX_WINDOW_DAYS,
    MISSING_ANY,
    PRESENT,
    READ_WINDOW_DAYS,
    REVENUE_WINDOW_DAYS,
)

NO_MERCHANT = "No merchant account is linked to this user."

MAX_READ_DAYS = 90

DAYS_PARAM = OpenApiParameter(
    "days", OpenApiTypes.INT,
    description=f"Default {REVENUE_WINDOW_DAYS}. {DAYS_HELP}")
READ_DAYS_PARAM = OpenApiParameter(
    "read_days", OpenApiTypes.INT,
    description=f"Window for the catalogue-reads card only, default "
                f"{READ_WINDOW_DAYS}, max {MAX_READ_DAYS}. Separate from `days` "
                f"because the card reads 'last 7 days' while the revenue chart "
                f"draws 14 bars.")
MISSING_PARAM = OpenApiParameter(
    "missing", OpenApiTypes.STR, enum=[*PRODUCT_DATA_FIELDS, MISSING_ANY],
    description=f"Return the agent-visible rows missing this one detail, instead "
                f"of the counts. This is the number the card prints, as rows. "
                f"`{MISSING_ANY}` returns the rows missing at least one known "
                f"detail -- the union of the single-detail lists, each row once.")


class _DashboardAV(BaseAV):
    """Resolve the caller's store, or answer 404.

    404 and not 403, matching _OrdersAV and the policy and audit surfaces:
    which status code comes back must not be a way to learn what exists.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None

    def _days(self, request, default, *, param="days", maximum=MAX_WINDOW_DAYS):
        return dash.clamp_days(request.query_params.get(param), default, maximum)

    def _revenue(self, tenant, days, now):
        """The revenue series, the agent bars and the prior window, together.

        One helper shared by the combined call, /dashboard/revenue and
        /dashboard/agents rather than one aggregate each: the bars have to sum
        to the line, and three endpoints computing the same figures separately
        is how they stop.
        """
        payload = dash.revenue_and_agents(tenant, days=days, now=now)
        payload["prior_window"] = dash.basket_comparison(tenant, days=days, now=now)
        return payload


class DashboardAV(_DashboardAV):
    """The whole screen in one call.

    One request rather than six, for the reason PolicyOverviewAV states: the
    cards all describe the same moment, and assembled from separate calls the
    screen can render a payments total that disagrees with the bars beneath it.

    `now` is taken ONCE and threaded through every function, so no two cards can
    land on opposite sides of midnight. On a dashboard whose windows are
    calendar days that is a real outcome at 00:00 local, not a theoretical one.
    """

    @extend_schema(parameters=[DAYS_PARAM, READ_DAYS_PARAM])
    @extend_response(DashboardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        now = timezone.now()
        days = self._days(request, REVENUE_WINDOW_DAYS)
        read_days = self._days(request, READ_WINDOW_DAYS,
                               param="read_days", maximum=MAX_READ_DAYS)

        policy_card = policy_analytics.store_card(tenant)

        return {
            "merchant": {
                "domain": tenant.domain,
                "business_name": tenant.business_name,
                "currency": tenant.currency,
                "agent_traffic_enabled": tenant.agent_traffic_enabled,
            },
            "generated_at": now,
            "payments": dash.payments(tenant, now=now),
            "orders": dash.orders(tenant, now=now),
            "revenue": self._revenue(tenant, days, now),
            "catalog_reads": dash.catalog_reads(tenant, days=read_days, now=now),
            "products": dash.products(tenant, required_fields=_required(policy_card), now=now),
            "policy": policy_card,
        }


def _required(policy_card) -> list:
    """Which details the products card should hold a row against.

    [] and not the six defaults when the rule is not enforcing: an incomplete
    row refuses nothing while the rule is a draft, so counting it as unsellable
    would show a merchant a gap that costs them nothing to leave alone. With
    this, "Ready to sell" means "would sell today" -- the only reading of that
    donut a merchant will not be misled by.
    """
    required = policy_card["required_product_fields"]
    return required["fields"] if required["enforcing"] else []


class DashboardPaymentsAV(_DashboardAV):
    """Money received, all time, with the month either side for context.

    The settlement block is the honest version of the design's
    "Settled straight to acc_QK3n...4471": that Razorpay account id is
    permanently unavailable, so this returns the masked key id and a sentence
    composed server-side. See dashboard_analytics.settlement.
    """

    @extend_response(PaymentsCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        return dash.payments(tenant)


class DashboardRevenueAV(_DashboardAV):
    """The daily revenue series, plus the window before it to compare against.

    Exactly `days` points, zero-filled and in order, so the chart cannot skip a
    date and the client never has to guess which day a gap belongs to.
    """

    @extend_schema(parameters=[DAYS_PARAM])
    @extend_response(RevenueCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        now = timezone.now()
        return self._revenue(tenant, self._days(request, REVENUE_WINDOW_DAYS), now)


class DashboardAgentsAV(_DashboardAV):
    """Which AI placed the orders, by revenue and count.

    The same payload as /dashboard/revenue, from the same aggregate, because the
    bars and the line are one set of facts and the two cards are two readings of
    it. Kept as its own route so the per-agent card can be refreshed on its own
    window without the client having to know it shares an endpoint with the
    chart. Every `agent` key is a valid ?agent= value on GET /orders/, which is
    what makes a bar clickable.

    What this cannot tell a merchant is how agent baskets compare to their
    storefront ones: create_order has exactly one caller, so every order in
    this system is an agent order and there is no storefront denominator.
    `prior_window` is the comparison that does exist.
    """

    @extend_schema(parameters=[DAYS_PARAM])
    @extend_response(RevenueCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        now = timezone.now()
        return self._revenue(tenant, self._days(request, REVENUE_WINDOW_DAYS), now)


class DashboardCatalogReadsAV(_DashboardAV):
    """How often agents read this store's catalogue, by day, agent and tool."""

    @extend_schema(parameters=[
        OpenApiParameter("days", OpenApiTypes.INT,
                         description=f"Default {READ_WINDOW_DAYS}, max "
                                     f"{MAX_READ_DAYS}. {DAYS_HELP}"),
    ])
    @extend_response(CatalogReadsCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        days = self._days(request, READ_WINDOW_DAYS, maximum=MAX_READ_DAYS)
        return dash.catalog_reads(tenant, days=days)


class DashboardProductsAV(_DashboardAV):
    """Sellable vs total, and what the gap is waiting on.

    Holds each row against the details the store CURRENTLY enforces, so the
    "Ready to sell" figure means "would sell today" rather than "satisfies a
    rule that is still a draft". See _required.
    """

    @extend_response(ProductsCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        policy_card = policy_analytics.store_card(tenant)
        return dash.products(tenant, required_fields=_required(policy_card))


class DashboardProductRowsAV(_DashboardAV):
    """The rows behind the number: agent-visible products missing one detail.

    The arrow on "4 waiting on one detail each" exists because the merchant
    wants those four products, not the number four. Its own route rather than a
    ?missing= mode on the card above, so each operation has one response shape
    the schema can describe honestly -- a single path answering either counts or
    a paginated row list documents as neither.

    Filters over every KNOWN detail, not only the required ones: a row can be
    missing a detail the current policy does not ask for, and a merchant fixing
    their catalogue wants to see that before they tighten the rule. Built from
    dashboard_analytics.visible_products and the same PRESENT clause the card
    counts with, so the list and the number cannot disagree.

    ?missing=any is the same list without having to name the detail first: every
    visible row missing at least one known detail, each row once however many it
    is missing. That is the whole "52 - 48" gap when the store requires all six,
    and a superset of it when it requires fewer -- the same reason the
    single-detail lists filter over every known field rather than the required
    ones. It excludes with ALL_PRESENT, the conjunction of the very clauses the
    per-detail lists exclude with, so `any` cannot list a row that no
    single-detail list would.
    """

    @extend_schema(parameters=[
        MISSING_PARAM,
        OpenApiParameter("page", OpenApiTypes.INT),
        OpenApiParameter("page_size", OpenApiTypes.INT),
    ])
    @extend_response(ProductRowsSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        missing = request.query_params.get("missing", "any")
        if missing != MISSING_ANY and missing not in PRESENT:
            return Response(
                {"msg": f"Unknown detail {missing!r}. "
                        f"One of: {', '.join([*PRESENT, MISSING_ANY])}."},
                status=status.HTTP_400_BAD_REQUEST)

        present = ALL_PRESENT if missing == MISSING_ANY else PRESENT[missing]
        rows = (dash.visible_products(tenant)
                .exclude(present)
                .order_by("-fetched_at", "-id"))
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response(
            product_list_serializer(page, many=True).data)
        response.data["missing"] = missing
        return response


class DashboardPolicyAV(_DashboardAV):
    """The store-policy panel on its own.

    A thin read for a client that wants to refresh that panel without the whole
    screen. The panel's "open" arrow goes to GET /policy/overview/, which
    returns every policy, every rule and the policy tiles -- this deliberately
    duplicates none of that, and is not the place to add an editor.
    """

    @extend_response(PolicyCardSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        return policy_analytics.store_card(tenant)
