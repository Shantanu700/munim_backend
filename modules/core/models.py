import hashlib
import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import IntegrityError, models, transaction

from base.defaults import DEFAULT_ON_DELETE
from base.fields import EncryptedCharField
from base.manager import UserManager
from base.model import DropdownBase, ModelBase
from modules.core.choices import UserRoleChoices


def upload_file(instance, filename: str):
    return "uploads/{0}/{1}".format(instance.uuid, filename)


class UploadFile(ModelBase):
    file = models.FileField(upload_to=upload_file)


class MasterDropdown(DropdownBase):
    class Meta(DropdownBase.Meta):
        db_table = "core_dropdown"


class User(ModelBase, AbstractUser):
    USER_MODEL_FIELDS = ModelBase.BASE_MODEL_FIELDS + (
        "is_superuser",
        "last_login",
        "is_staff",
        "is_active",
        "date_joined",
        "groups",
        "user_permissions",
    )

    username = models.CharField(
        max_length=150,
        unique=True,
    )
    name = models.CharField(
        max_length=255,
    )
    user_role = models.SmallIntegerField(
        choices=UserRoleChoices.choices,
        default=UserRoleChoices.BUYER,
    )
    email = models.EmailField(unique=True)
    google_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    profile_image = models.ForeignKey(
        UploadFile,
        on_delete=DEFAULT_ON_DELETE,
        null=True,
        related_name="user_profile_images",
    )
    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def get_merchant(self) -> "Merchant | None":
        try:
            return self.merchant
        except Merchant.DoesNotExist:
            return None


class Merchant(ModelBase):
    """One onboarded store. The tenant.

    Payment credentials are the MERCHANT's, not ours. Every Payment Link is
    created on their Razorpay account, so funds settle directly to them and
    never pass through Munim -- holding other people's money is payment
    aggregation, which in India is a licensed activity we have no business
    performing. It also makes the pitch literally true: agent orders settle on
    the merchant's own Razorpay rails.

    The three razorpay_* credentials are encrypted at rest (base/fields.py). That
    defends a stolen dump, a read replica and the query log; it does not defend a
    compromised host, since the key lives in the same .env this process reads. Two
    consequences worth knowing before you go looking: `SELECT *` in psql shows a
    Fernet token rather than the secret, and `dumpdata` still emits cleartext,
    because serialisation reads the attribute rather than the column.
    """
    user = models.OneToOneField(
        User,
        on_delete=DEFAULT_ON_DELETE,
        related_name="merchant",
    )
    domain = models.CharField(max_length=255, unique=True, null=True)
    business_name = models.CharField(max_length=255, null=True)

    razorpay_key_id = EncryptedCharField(max_plaintext_length=64, null=True)
    razorpay_key_secret = EncryptedCharField(max_plaintext_length=128, null=True)
    razorpay_webhook_secret = EncryptedCharField(
        max_plaintext_length=128, null=True,
        help_text="Invented by the merchant when creating the webhook, not issued by Razorpay",
    )

    razorpay_account_id = models.CharField(max_length=64, null=True, blank=True)
    razorpay_account_name = models.CharField(max_length=255, null=True, blank=True)
    razorpay_verified_at = models.DateTimeField(null=True, blank=True)

    agent_traffic_enabled = models.BooleanField(
        default=True,
        help_text="Off means no agent buys anything, at all. Overrides every policy.",
    )
    currency = models.CharField(max_length=3, default="INR")

    order_seq = models.BigIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.domain

    @property
    def is_payment_ready(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def razorpay_key_mode(self) -> str | None:
        """'live', 'test', or None.

        The key_id prefix is the only self-description a standard Razorpay key
        carries -- there is no API that will tell you which mode a key belongs to.
        """
        key_id = self.razorpay_key_id or ""
        for mode in ("live", "test"):
            if key_id.startswith(f"rzp_{mode}_"):
                return mode
        return None

    @property
    def webhook_url(self) -> str | None:
        if not self.domain:
            return None
        return f"https://{settings.PUBLIC_HOST}/webhooks/razorpay/{self.domain}"

    @property
    def mcp_url(self) -> str | None:
        if not self.domain:
            return None
        return f"https://{settings.PUBLIC_HOST}/mcp/{self.domain}"

class MerchantCategory(ModelBase):
    """A merchant's own category, for their own dropdowns.

    The engine does not know or care about these. They are a convenience for
    the merchant to avoid re-typing the same string over and over in their
    own dropdowns.
    """
    tenant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="+")
    key = models.SlugField(max_length=255, null=True)
    name = models.CharField(max_length=255)

    class Meta:
        unique_together = ("tenant", "name")






class Buyer(ModelBase):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="buyer_profile")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

    @property
    def display_name(self) -> str:
        return self.name or self.email.split("@")[0]

    @classmethod
    def for_user(cls, user) -> "Buyer":
        """The Buyer row behind a logged-in user, created or adopted on first use.

        Three things make this longer than a get_or_create.

        unfiltered_objects, not objects: ModelBase's default manager hides
        status=DELETED rows, but a soft-deleted Buyer still holds the unique
        user_id and the unique email in the database. Looking through `objects`
        finds nothing, then create() dies on a constraint pointing at a row the
        caller cannot see.

        Adoption by email, because nothing in the request path has ever created a
        Buyer -- but the CLI and the fixtures have, and those rows are the ones
        already attached to issued mandates. A user whose email matches one must
        inherit it rather than get a second identity, or their existing mandates
        stay invisible forever, which is the exact failure this link exists to
        fix. Buyer.email and User.email are both unique EmailFields that Postgres
        compares case-sensitively, so the adopt lookup is iexact while the row
        this creates stores the user's email verbatim.

        And the IntegrityError branch: two requests from one freshly-registered
        user race here, both find nothing, and one loses on the unique index.
        Re-read and return the winner's row -- inside a savepoint, so losing does
        not poison an enclosing transaction.
        """
        existing = cls.unfiltered_objects.filter(user=user).first()
        if existing is not None:
            return existing

        orphan = cls.unfiltered_objects.filter(
            email__iexact=user.email, user__isnull=True).first()
        if orphan is not None:
            cls.unfiltered_objects.filter(pk=orphan.pk).update(user=user)
            orphan.user = user
            return orphan

        try:
            with transaction.atomic():
                return cls.objects.create(email=user.email, name=user.name or "",
                                          user=user)
        except IntegrityError:
            return cls.unfiltered_objects.get(user=user)

class TenantScoped(ModelBase):
    """Base for every tenant-owned table (handoff §3: tenant_id on EVERY table)."""
    tenant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True


class PlatformAPIKey(ModelBase):
    """A buyer's personal key for the platform-wide MCP endpoint (bare /mcp).

    Unlike Merchant's Razorpay credentials -- issued by Razorpay, plaintext
    because that format isn't ours to change -- this secret's shape is ours to
    choose, and hashing it costs nothing. Only the hash is stored; the raw key
    is returned once, at issue(), and is not retrievable again.
    """
    KEY_PREFIX = "munim_sk_"

    user = models.ForeignKey(User, on_delete=DEFAULT_ON_DELETE, related_name="platform_api_keys")
    name = models.CharField(max_length=100, blank=True)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=24, editable=False,
                              help_text="Leading characters of the raw key, for admin display only")
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.prefix}... ({self.user.email})"

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None

    @staticmethod
    def _hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, user) -> tuple["PlatformAPIKey", str]:
        """Create a new key. Returns (row, raw_key) -- raw_key is shown exactly once."""
        raw = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        row = cls.objects.create(user=user, key_hash=cls._hash(raw), prefix=raw[:len(cls.KEY_PREFIX) + 6])
        return row, raw

    @classmethod
    def resolve(cls, raw_key: str) -> "PlatformAPIKey | None":
        """Look up a live key by its raw value. None on unknown, garbled, or revoked."""
        if not raw_key or not raw_key.startswith(cls.KEY_PREFIX):
            return None
        return cls.objects.filter(key_hash=cls._hash(raw_key), revoked_at__isnull=True).first()
