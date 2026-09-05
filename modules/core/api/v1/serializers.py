from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils.text import slugify
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.core.choices import UserRoleChoices
from modules.core.models import (
    MasterDropdown,
    Merchant,
    MerchantCategory,
    PlatformAPIKey,
    User,
)


class DropDownSerializer(BaseSerializer):
    key = serializers.SerializerMethodField()
    class Meta:
        model = MasterDropdown
        fields = "__all__"

    def get_key(self, obj):
        return slugify(obj.label)

class MerchantCategorySerializer(BaseSerializer):
    """One of a store's own categories, as its dropdowns render it.

    `key` is derived from the name when the stored column is empty. The column
    is nullable and only filled by the crawler, so a row written any other way
    -- a fixture, the admin, a category whose name slugified to nothing -- would
    otherwise hand the dropdown a null where it expects a value to send back.
    """

    key = serializers.SerializerMethodField()

    class Meta:
        model = MerchantCategory
        fields = ("uuid", "key", "name")

    def get_key(self, obj) -> str:
        return obj.key or slugify(obj.name)[:255]


class MerchantDropdownSerializer(BaseSerializer):
    """One onboarded store, as a picker renders it.

    Three fields and no more: the uuid is what the caller sends back, and the
    domain and the business name are what a human reads to tell two stores
    apart. Nothing about payment credentials or readiness belongs in a list
    that names every store on the platform.
    """

    class Meta:
        model = Merchant
        fields = ("uuid", "domain", "business_name")


class MerchantSerializer(BaseSerializer):
    webhook_url = serializers.ReadOnlyField()
    mcp_url = serializers.ReadOnlyField()
    uuid = serializers.ReadOnlyField()

    class Meta:
        model = Merchant
        fields = ("domain", "business_name", "is_payment_ready", "webhook_url", "mcp_url", "uuid")


class LoginSerializer(BaseSerializer):
    file_fields = ["profile_image"]
    merchant = serializers.SerializerMethodField()

    class Meta:
        model = User
        exclude = User.USER_MODEL_FIELDS + ("password",)

    @extend_schema_field(PolymorphicProxySerializer(
        component_name="LoginMerchant",
        serializers=[MerchantSerializer],
        resource_type_field_name=None,
        many=False,
        allow_null=True,
    ))
    def get_merchant(self, obj):
        merchant = obj.get_merchant()
        return MerchantSerializer(merchant).data if merchant else None


class RegisterSerializer(BaseSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("username", "name", "email", "password", "user_role")

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        if user.user_role == UserRoleChoices.MERCHANT:
            Merchant.objects.create(user=user)
        return user


class PlatformAPIKeyCreateSerializer(serializers.Serializer):
    "Input for issuing a new platform API key"

    name = serializers.CharField(required=False, allow_blank=True, max_length=100)


class PlatformAPIKeyIssuedSerializer(serializers.Serializer):
    "Shown exactly once, at creation -- the raw key is never retrievable again"

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True, allow_blank=True)
    key = serializers.CharField(read_only=True)


class PlatformAPIKeySerializer(BaseSerializer):
    class Meta:
        model = PlatformAPIKey
        fields = ("uuid", "name", "prefix", "last_used_at", "revoked_at", "created_at")


class WebhookEventSerializer(serializers.Serializer):
    "One event the merchant should subscribe to in the Razorpay dashboard"

    event = serializers.CharField()
    required = serializers.BooleanField()
    why = serializers.CharField()


class RazorpayStateSerializer(serializers.Serializer):
    """Everything the Razorpay payout screen draws, in one response.

    Note what is and is not here. `key_secret` never appears in any response: Razorpay
    shows it once, the merchant already has it, and we only need to say whether one is
    on file. `webhook_secret` DOES appear, because we generated it rather than Razorpay
    -- a merchant who comes back tomorrow to finish configuring the dashboard has to be
    able to read it, and minting a fresh one would silently break a webhook they had
    already set up. Both are encrypted at rest regardless.
    """

    domain = serializers.CharField(allow_null=True)
    business_name = serializers.CharField(allow_null=True)

    key_id = serializers.CharField(allow_null=True)
    key_secret_set = serializers.BooleanField()
    key_mode = serializers.CharField(allow_null=True)
    is_payment_ready = serializers.BooleanField()

    verified_at = serializers.DateTimeField(allow_null=True)
    account_id = serializers.CharField(allow_null=True)
    account_name = serializers.CharField(allow_null=True)

    webhook_url = serializers.CharField(allow_null=True)
    webhook_secret = serializers.CharField(allow_null=True)
    webhook_events = WebhookEventSerializer(many=True)
    webhook_received = serializers.BooleanField()
    last_event_type = serializers.CharField(allow_null=True)
    last_event_at = serializers.DateTimeField(allow_null=True)
    last_event_was_test = serializers.BooleanField()


def max_bytes(limit):
    """Bound a field by UTF-8 bytes, which is what the encrypted column budgets.

    DRF's max_length counts characters, and the two disagree the moment anyone
    pastes something non-ASCII: 128 characters of accented text is 256 bytes, whose
    Fernet token overflows varchar(268) and surfaces as a 500 from deep inside the
    field. A paste is user input, so it has to come back as a 400 that says what
    was wrong.
    """
    def validator(value):
        size = len(value.encode("utf-8"))
        if size > limit:
            raise serializers.ValidationError(
                f"This value is {size} bytes long and the limit is {limit}. "
                f"A Razorpay credential is plain ASCII -- check for stray characters "
                f"from the paste.")
    return validator


class RazorpayCredentialsWriteSerializer(serializers.Serializer):
    """Partial on purpose, matching the house PUT convention.

    A merchant re-verifying an existing pair should not have to re-type the secret
    just to correct a typo in the key id.
    """

    key_id = serializers.CharField(required=False, max_length=64,
                                   validators=[max_bytes(64)])
    key_secret = serializers.CharField(required=False, max_length=128, write_only=True,
                                       validators=[max_bytes(128)],
                                       style={"input_type": "password"})
    webhook_secret = serializers.CharField(required=False, max_length=128,
                                           validators=[max_bytes(128)],
                                           write_only=True)

    def validate_key_id(self, value):
        value = value.strip()
        if not value.startswith(("rzp_live_", "rzp_test_")):
            raise serializers.ValidationError(
                "A Razorpay key id starts with rzp_live_ or rzp_test_. This looks like "
                "the key secret or something else -- the key id is the visible half, "
                "shown in the dashboard under Account & Settings -> API Keys.")
        return value

    def validate_key_secret(self, value):
        value = value.strip()
        if value.startswith(("rzp_live_", "rzp_test_")):
            raise serializers.ValidationError(
                "That is a key id, not a key secret. The secret is shown only once, "
                "when the key pair is generated.")
        return value

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Send at least one of key_id, key_secret or webhook_secret.")
        return attrs


class RazorpayVerifyRequestSerializer(serializers.Serializer):
    """Optional overrides, so a pair can be checked before it is committed.

    Same key_id validation as the write serializer, and for a sharper reason than
    tidiness: this endpoint's key_id reaches LedgerEntry.detail, which is append-only.
    Refusing a value that is not shaped like a key id is what stops a secret pasted
    into the wrong box from becoming a permanent row.
    """

    key_id = serializers.CharField(required=False, max_length=64,
                                   validators=[max_bytes(64)])
    key_secret = serializers.CharField(required=False, max_length=128, write_only=True,
                                       validators=[max_bytes(128)],
                                       style={"input_type": "password"})

    validate_key_id = RazorpayCredentialsWriteSerializer.validate_key_id
    validate_key_secret = RazorpayCredentialsWriteSerializer.validate_key_secret


class RazorpayVerifyResponseSerializer(serializers.Serializer):
    """The outcome of a verification attempt.

    `source` is "probe" when the pair authenticates: Razorpay does not expose the
    account name to a standard key, so the name on screen is the one on file here.
    """

    ok = serializers.BooleanField()
    account_id = serializers.CharField(allow_null=True)
    business_name = serializers.CharField(allow_null=True)
    mode = serializers.CharField(allow_null=True)
    source = serializers.CharField(allow_null=True)
    message = serializers.CharField(allow_blank=True)
    verified_at = serializers.DateTimeField(allow_null=True)


class RazorpayWebhookSecretSerializer(serializers.Serializer):
    "A freshly minted webhook secret, and where it has to be pasted"

    webhook_url = serializers.CharField()
    webhook_secret = serializers.CharField()
    webhook_events = WebhookEventSerializer(many=True)
    replaced = serializers.BooleanField()
    message = serializers.CharField()


class RazorpayTestEventSerializer(serializers.Serializer):
    "Result of the self-signed round trip to the store's own webhook URL"

    ok = serializers.BooleanField()
    status = serializers.IntegerField(required=False, allow_null=True)
    event_id = serializers.CharField(required=False, allow_null=True)
    url = serializers.CharField(required=False, allow_null=True)
    message = serializers.CharField(required=False, allow_blank=True)
    error = serializers.CharField(required=False, allow_blank=True)


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField(write_only=True)

    def validate(self, attrs):

        claims = google_id_token.verify_oauth2_token(
            attrs["id_token"],
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
        attrs["id_token"] = claims
        return attrs
