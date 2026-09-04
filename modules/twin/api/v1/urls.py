from django.urls import path

from modules.twin.api.v1.views import RazorpayWebhookAV, WebhookHitListAV

urlpatterns = [
    path("razorpay", RazorpayWebhookAV.as_view()),
    path("razorpay/<str:merchant>", RazorpayWebhookAV.as_view()),
    path("hits", WebhookHitListAV.as_view()),
]
