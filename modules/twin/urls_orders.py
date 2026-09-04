from django.urls import path

from modules.twin.api.v1.order_views import (
    OrderCountsAV,
    OrderDetailAV,
    OrderExportAV,
    OrderListAV,
    OrderTilesAV,
)

urlpatterns = [
    path("", OrderListAV.as_view()),
    path("tiles", OrderTilesAV.as_view()),
    path("counts", OrderCountsAV.as_view()),
    path("export", OrderExportAV.as_view()),
    path("<uuid:order_uuid>", OrderDetailAV.as_view()),
]
