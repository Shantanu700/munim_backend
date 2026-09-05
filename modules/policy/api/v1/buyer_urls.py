"""The buyer's routes, mounted at /buyer/ rather than under /policy/.

/policy/ is the merchant's screen: every view there resolves
request.user.get_merchant() and answers a buyer's login 404 "No merchant account
is linked to this user". One prefix holding two opposite authorization models is
a prefix where the next reader has to check each view to know which one they are
looking at -- and drf-spectacular tags operations by first path segment, so these
would be filed under "policy" in the published schema. /orders/, /dashboard/,
/audit/ and /ingest/ already put the audience in the URL.

Lookup is by uuid, not jti. `jti` is the mandate's identity INSIDE the signed
token and is what the engine and the payment-link notes join on; `uuid` is the
row's public handle everywhere else in this API (policy_uuid, key_uuid,
order_uuid). Using jti here would put the token's own identifier into browser
history and referrers for no gain.
"""
from django.urls import path

from modules.policy.api.v1.mandate_views import (
    BuyerMandateDetailAV,
    BuyerMandateListCreateAV,
    BuyerMandateRevokeAV,
)
from modules.twin.api.v1.buyer_order_views import (
    BuyerOrderDetailAV,
    BuyerOrderListAV,
)

urlpatterns = [
    path("mandates/", BuyerMandateListCreateAV.as_view()),
    path("mandates/<uuid:mandate_uuid>/", BuyerMandateDetailAV.as_view()),
    path("mandates/<uuid:mandate_uuid>/revoke/", BuyerMandateRevokeAV.as_view()),
    path("orders/", BuyerOrderListAV.as_view()),
    path("orders/<uuid:order_uuid>/", BuyerOrderDetailAV.as_view()),
]
