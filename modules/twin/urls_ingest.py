from django.urls import path

from modules.twin.api.v1.views import IngestAV, ProductDetailAV, ProductListAV
from modules.twin.views_stream import IngestStreamView

urlpatterns = [
    path("", IngestAV.as_view()),
    path("products", ProductListAV.as_view()),
    path("products/<uuid:product_uuid>", ProductDetailAV.as_view()),
    path("<uuid:job_uuid>/stream", IngestStreamView.as_view()),
]
