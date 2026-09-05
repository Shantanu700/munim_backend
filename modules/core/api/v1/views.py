import base64
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
)
from rest_framework import status
from rest_framework.response import Response

from base.decorators import extend_response
from base.permissions import APIAuthenticationPermission, IsMerchantPermission
from base.views import BaseAV
from modules.core import services
from modules.core.api.v1.serializers import (
    DropDownSerializer,
    GoogleAuthSerializer,
    LoginSerializer,
    MerchantCategorySerializer,
    MerchantDropdownSerializer,
    PlatformAPIKeyCreateSerializer,
    PlatformAPIKeyIssuedSerializer,
    PlatformAPIKeySerializer,
    RazorpayCredentialsWriteSerializer,
    RazorpayStateSerializer,
    RazorpayTestEventSerializer,
    RazorpayVerifyRequestSerializer,
    RazorpayVerifyResponseSerializer,
    RazorpayWebhookSecretSerializer,
    RegisterSerializer,
)
from modules.core.models import (
    MasterDropdown,
    Merchant,
    MerchantCategory,
    PlatformAPIKey,
    User,
)

NO_MERCHANT = "No merchant account is linked to this user."
NO_DOMAIN = ("This store has no domain yet, so it has no webhook URL. Finish the "
             "catalogue step first.")
NO_DROPDOWN = "No dropdown exists with that label."
NO_MERCHANT_UUID = "No store exists with that UUID."


class LoginAV(BaseAV):
    "Login/Logout API View"

    authentication = {
        "post": False,
    }

    def decrypt_auth(self, meta_info) -> dict[str, Any]:
        header, data = meta_info.split(" ")
        if header != "Basic":
            return {}
        decrypted_auth = base64.b64decode(data).decode("utf-8")
        credentials = decrypted_auth.split(":")
        return {
            "email": credentials[0],
            "password": credentials[1],
        }

    @extend_response(LoginSerializer())
    def get(self, request):
        serializer = LoginSerializer(
            instance=request.user,
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="Authorization",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                examples=[
                    OpenApiExample(
                        name="User Authentication",
                        value="Basic ZXJwQGtpZXQuZWR1OkBlcnA=",
                        summary="base64 encoded credentials are required",
                        description="",
                    )
                ],
            )
        ]
    )
    @extend_response(LoginSerializer())
    def post(self, request):
        auth_data = request.META.get("HTTP_AUTHORIZATION")
        credentials = self.decrypt_auth(auth_data)
        user = authenticate(request, **credentials)
        if user is not None:
            login(request, user)
            response = {
                "msg": "Login Successful.",
            }
            return LoginSerializer(instance=user).data
        response = {
            "msg": "Invalid Credentials.",
        }
        return Response(response, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        logout(request)
        response = {
            "msg": "Logout successful.",
        }
        return Response(response, status=status.HTTP_200_OK)


class RegisterAV(BaseAV):
    "Sign Up API View"

    authentication = {
        "post": False,
    }

    @extend_schema(
        request=RegisterSerializer(),
        )
    @extend_response(LoginSerializer())
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        login(request, user)
        return LoginSerializer(instance=user).data


class MasterDropdownAV(BaseAV):
    @staticmethod
    def _parent(slug: str) -> MasterDropdown | None:
        slug = slug.lower()
        parent = MasterDropdown.objects.filter(
            label__iexact=slug.replace("-", " ")).order_by("id").first()
        return parent


    @extend_response(DropDownSerializer(many=True))
    def get(self, request, parent_slug):
        parent = self._parent(parent_slug)
        if parent is None:
            return Response({"msg": NO_DROPDOWN}, status=status.HTTP_404_NOT_FOUND)

        children = parent.children.order_by("id")
        return DropDownSerializer(children, many=True).data


class MerchantCategoryListAV(BaseAV):
    """The categories of one store, addressed by the merchant's UUID.

    Unauthenticated, deliberately: this is what an agent or a storefront reads
    to learn what a store sells, before there is any session to authenticate.
    Nothing here is private -- the names are the same ones the merchant's own
    catalogue pages already publish.

    A store that exists but has no categories yet is an empty list rather than
    a 404, matching MasterDropdownAV: "not crawled yet" is a legitimate answer,
    and only a UUID naming no store is a wrong request.
    """
    @extend_response(MerchantCategorySerializer(many=True))
    def get(self, request, merchant_uuid):
        tenant = Merchant.objects.filter(uuid=merchant_uuid).first()
        if tenant is None:
            return Response({"msg": NO_MERCHANT_UUID}, status=status.HTTP_404_NOT_FOUND)

        categories = MerchantCategory.objects.filter(tenant=tenant).order_by("name")
        return MerchantCategorySerializer(categories, many=True).data


class MerchantDropdownAV(BaseAV):
    """Every onboarded store, as a dropdown reads it: uuid, domain, name.

    Authenticated (BaseAV's default): MerchantCategoryListAV is open because a
    caller who already knows a store's UUID learns nothing new from its
    catalogue, but this route is the thing that hands out those UUIDs, and the
    roster of who is on the platform is not public.

    A store with no domain yet is left out. It has no MCP or webhook URL, so it
    cannot be transacted with, and its label would render as a blank row.
    """

    @extend_response(MerchantDropdownSerializer(many=True))
    def get(self, request):
        merchants = Merchant.objects.filter(domain__isnull=False).order_by("domain")
        return MerchantDropdownSerializer(merchants, many=True).data


class PlatformAPIKeyListCreateAV(BaseAV):
    """Issue, list, and revoke platform API keys for the calling user.

    Session-authenticated (BaseAV's default authentication = True): only a
    logged-in buyer/user can mint a key for themselves, to use with the
    platform-wide MCP endpoint. The raw key is returned once, on creation --
    only its hash is ever stored, so it cannot be shown again after this.
    """

    @extend_response(PlatformAPIKeySerializer(many=True))
    def get(self, request):
        keys = PlatformAPIKey.objects.filter(user=request.user)
        return PlatformAPIKeySerializer(keys, many=True).data

    @extend_schema(request=PlatformAPIKeyCreateSerializer())
    @extend_response(PlatformAPIKeyIssuedSerializer())
    def post(self, request):
        serializer = PlatformAPIKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row, raw_key = PlatformAPIKey.issue(request.user)
        name = serializer.validated_data.get("name")
        if name:
            row.name = name
            row.save(update_fields=["name"])
        return PlatformAPIKeyIssuedSerializer({"uuid": row.uuid, "name": row.name, "key": raw_key}).data


class PlatformAPIKeyDeleteAV(BaseAV):

    def delete(self, request, key_uuid):
        row = PlatformAPIKey.objects.filter(user=request.user, uuid=key_uuid).first()
        if row is None:
            return Response({"msg": "No such key."}, status=status.HTTP_404_NOT_FOUND)
        row.revoked_at = timezone.now()
        row.save(update_fields=["revoked_at"])
        return Response({"msg": "Revoked."}, status=status.HTTP_200_OK)


class _MerchantAV(BaseAV):
    """Resolve the caller's store.

    A caller with no store never reaches a handler here -- IsMerchantPermission
    answers 403 first. Where a store does exist, 404 and not 403 remains the rule
    for a row belonging to someone else, matching every other tenant view in the
    codebase: which status code comes back should not be a way to learn what
    exists.
    """

    authentication = True
    permission_classes = [APIAuthenticationPermission, IsMerchantPermission]

    def _tenant(self, request):
        tenant = request.user.get_merchant()
        if tenant is None:
            return None, Response({"msg": NO_MERCHANT}, status=status.HTTP_404_NOT_FOUND)
        return tenant, None


class RazorpayAccountAV(_MerchantAV):
    """The store's Razorpay linkage: read the whole setup panel, or save credentials.

    GET returns the entire screen in one call, the way IngestAV.get does for the
    catalogue step -- including whether a webhook has ever arrived, so the "waiting
    for the first webhook" line resolves by re-polling this rather than needing a
    route of its own. The two halves of the screen describe one moment; fetched
    separately they can disagree.
    """

    @extend_response(RazorpayStateSerializer())
    def get(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        return {
            "domain": tenant.domain,
            "business_name": tenant.business_name,
            "key_id": tenant.razorpay_key_id,
            "key_secret_set": bool(tenant.razorpay_key_secret),
            "key_mode": tenant.razorpay_key_mode,
            "is_payment_ready": tenant.is_payment_ready,
            "verified_at": tenant.razorpay_verified_at,
            "account_id": tenant.razorpay_account_id,
            "account_name": tenant.razorpay_account_name,
            "webhook_url": tenant.webhook_url,
            "webhook_secret": tenant.razorpay_webhook_secret,
            "webhook_events": services.WEBHOOK_EVENTS,
            **services.webhook_arrival(tenant),
        }

    @extend_schema(request=RazorpayCredentialsWriteSerializer())
    @extend_response(RazorpayStateSerializer())
    def put(self, request):
        """Partial, matching the house convention: correcting the key id must not
        require re-typing a secret Razorpay will never show again."""
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        body = RazorpayCredentialsWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        services.save_credentials(tenant, by_user=request.user, **body.validated_data)
        return self.get(request)


class RazorpayVerifyAV(_MerchantAV):
    """Prove the stored key pair actually authenticates against Razorpay.

    Accepts an inline pair so the UI can check before committing. A rejected key is
    200 with ok:false, not a 4xx -- the request was well formed and was answered;
    the credentials are what failed, and that is a fact about the payload rather
    than an error in the call.
    """

    @extend_schema(request=RazorpayVerifyRequestSerializer())
    @extend_response(RazorpayVerifyResponseSerializer())
    def post(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        body = RazorpayVerifyRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        result = services.verify_and_record(tenant, by_user=request.user,
                                            **body.validated_data)
        tenant.refresh_from_db()
        return Response({**result, "verified_at": tenant.razorpay_verified_at},
                        status=status.HTTP_200_OK)


class RazorpayWebhookSecretAV(_MerchantAV):
    """Mint the webhook signing secret.

    Razorpay does not issue this one -- the merchant invents it when creating the
    webhook -- so generating it here is what makes both sides match without asking
    anyone to copy a string in two directions.
    """

    @extend_schema(request=None)
    @extend_response(RazorpayWebhookSecretSerializer())
    def post(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error
        if not tenant.domain:
            return Response({"msg": NO_DOMAIN}, status=status.HTTP_404_NOT_FOUND)

        replaced = bool(tenant.razorpay_webhook_secret)
        secret = services.rotate_webhook_secret(tenant, by_user=request.user)
        return {
            "webhook_url": tenant.webhook_url,
            "webhook_secret": secret,
            "webhook_events": services.WEBHOOK_EVENTS,
            "replaced": replaced,
            "message": ("This replaces the previous secret. Update it in Razorpay now "
                        "-- until you do, every webhook will fail signature verification."
                        if replaced else
                        "Copy this into Razorpay when you create the webhook."),
        }


class RazorpayTestEventAV(_MerchantAV):
    """Send a signed, synthetic event to the store's own webhook URL.

    Munim cannot ask Razorpay to send anything, so it sends what Razorpay would,
    signed the way Razorpay would sign it, over the public internet. That is what
    makes it worth having: it exercises DNS, TLS, the proxy, the route and the
    stored secret together, which is the actual list of things broken when a
    merchant reports that webhooks never arrive.
    """

    @extend_schema(request=None)
    @extend_response(RazorpayTestEventSerializer())
    def post(self, request):
        tenant, error = self._tenant(request)
        if error is not None:
            return error

        result = services.send_test_event(tenant, by_user=request.user)
        return Response(result, status=status.HTTP_200_OK)


def _generate_username_from_email(email: str) -> str:
    base = email.split("@")[0][:150]
    username = base
    suffix = 0
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base[:150 - len(str(suffix)) - 1]}-{suffix}"
    return username


class GoogleAuthAV(BaseAV):
    "Google Login/Sign Up API View"

    authentication = {
        "post": False,
    }

    @extend_schema(
        request=GoogleAuthSerializer,
    )
    @extend_response(LoginSerializer())
    def post(self, request):
        serializer = GoogleAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claims = serializer.validated_data["id_token"]

        user = User.objects.filter(google_id=claims["sub"]).first()
        if user is None:
            user = User.objects.filter(email=claims["email"]).first()
            if user is not None:
                user.google_id = claims["sub"]
                user.save()
        if user is None:
            user = User.objects.create_user(
                username=_generate_username_from_email(claims["email"]),
                email=claims["email"],
                name=claims.get("name", ""),
                password=None,
                google_id=claims["sub"],
            )

        login(request, user)
        return LoginSerializer(instance=user).data
