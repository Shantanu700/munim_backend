from typing import cast

from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

class APIAuthenticationPermission(BasePermission):
    message = "You are unauthenticated! Please login again!"
    code = status.HTTP_401_UNAUTHORIZED

    def has_permission(self, request: Request, view):
        authentication = getattr(view, "authentication", True)
        method = cast(str, request.method).lower()

        if not (
            authentication
            if isinstance(authentication, bool)
            else authentication.get(method, True)
        ):
            return True

        return bool(request.user and request.user.is_authenticated)


class IsMerchantPermission(BasePermission):
    """The caller owns a store.

    403 and not the 404 the tenant views use: those answer 404 so that a
    merchant cannot learn another store's row exists by watching the status
    code. That reasoning does not apply here -- this says nothing about any
    particular store, only that the caller does not own one -- so the honest
    answer is "not for you".

    Deliberately keyed on the Merchant row rather than on user_role, because
    the row is what every handler behind this permission goes on to resolve.
    Gating on the role would let the two disagree, and the disagreement would
    surface as a 403 on an endpoint whose body would have worked fine.
    """

    message = "You are not authorized to access this page!"
    code = status.HTTP_403_FORBIDDEN

    def has_permission(self, request: Request, view):
        user = getattr(request, "user", None)

        if not (user and user.is_authenticated):
            return False

        get_merchant = getattr(user, "get_merchant", None)
        return get_merchant is not None and get_merchant() is not None
