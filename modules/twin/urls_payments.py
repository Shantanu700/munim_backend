from django.urls import path

from modules.twin.api.v1.payment_views import PaymentDetailAV, PaymentListAV

urlpatterns = [
    path("", PaymentListAV.as_view()),
    path("<uuid:payment_uuid>", PaymentDetailAV.as_view()),
]
