from django.urls import path

from modules.core.api.v1.views import (
    GoogleAuthAV,
    LoginAV,
    MasterDropdownAV,
    MerchantCategoryListAV,
    MerchantDropdownAV,
    PlatformAPIKeyDeleteAV,
    PlatformAPIKeyListCreateAV,
    RazorpayAccountAV,
    RazorpayTestEventAV,
    RazorpayVerifyAV,
    RazorpayWebhookSecretAV,
    RegisterAV,
)


urlpatterns = [
    path(
        "login/",
        LoginAV.as_view(),
    ),
    path(
        "register/",
        RegisterAV.as_view(),
    ),
    path(
        "google/",
        GoogleAuthAV.as_view(),
    ),
    path(
        "dropdown/<slug:parent_slug>/",
        MasterDropdownAV.as_view(),
    ),
    path(
        "merchants/dropdown/",
        MerchantDropdownAV.as_view(),
    ),
    path(
        "merchants/<uuid:merchant_uuid>/categories/",
        MerchantCategoryListAV.as_view(),
    ),
    path(
        "api-keys/",
        PlatformAPIKeyListCreateAV.as_view(),
    ),
    path(
        "api-keys/<uuid:key_uuid>",
        PlatformAPIKeyDeleteAV.as_view(),
    ),
    path(
        "razorpay/",
        RazorpayAccountAV.as_view(),
    ),
    path(
        "razorpay/verify/",
        RazorpayVerifyAV.as_view(),
    ),
    path(
        "razorpay/webhook-secret/",
        RazorpayWebhookSecretAV.as_view(),
    ),
    path(
        "razorpay/test-event/",
        RazorpayTestEventAV.as_view(),
    ),
]
