from django.urls import path

from modules.twin.api.v1.dashboard_views import (
    DashboardAgentsAV,
    DashboardAV,
    DashboardCatalogReadsAV,
    DashboardPaymentsAV,
    DashboardPolicyAV,
    DashboardProductRowsAV,
    DashboardProductsAV,
    DashboardRevenueAV,
)

urlpatterns = [
    path("", DashboardAV.as_view()),
    path("payments", DashboardPaymentsAV.as_view()),
    path("revenue", DashboardRevenueAV.as_view()),
    path("agents", DashboardAgentsAV.as_view()),
    path("catalog-reads", DashboardCatalogReadsAV.as_view()),
    path("products", DashboardProductsAV.as_view()),
    path("products/rows", DashboardProductRowsAV.as_view()),
    path("policy", DashboardPolicyAV.as_view()),
]
