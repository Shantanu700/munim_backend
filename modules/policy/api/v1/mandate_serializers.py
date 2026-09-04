"""The buyer's side of a mandate: what they type, and what they get back.

Rupees on the wire in, rupees AND paise out. Rupees in because that is the number
on the buyer's screen, and a client that has to multiply by 100 is a client that
will one day do it in binary floating point -- the conversion happens once, here,
in Decimal. Paise out because paise is what the engine compares and what the
ledger records, so the authoritative number stays on the wire next to the display
one.

Nothing here calls save(). Every write goes through modules/policy/services.py,
for the reasons that module's docstring gives -- and one more that is specific to
mandates: issuing one signs a token, and a signature minted inside a serializer
is a signature nobody reading the view can see happen.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.policy.models import Mandate
from modules.policy.services import MAX_TTL_HOURS

MIN_RUPEES = Decimal("0.01")


def _paise(rupees: Decimal) -> int:
    """DecimalField has already refused a third decimal place, so this is exact."""
    return int((rupees * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(**kwargs):
    return serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=MIN_RUPEES, **kwargs)


class MandateWriteSerializer(serializers.Serializer):
    """What a buyer sends to authorise one agent on one store."""

    domain = serializers.CharField(max_length=255)

    subject = serializers.RegexField(
        r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$", max_length=64,
        help_text="Which agent this authorises, e.g. chatgpt or claude")

    per_txn_cap = _money(
        help_text="Rupees per purchase, above which the buyer is asked")
    total_cap = _money(
        help_text="Rupees in total across the mandate's whole life")
    step_up_ceiling = _money(
        help_text="Rupees above which not even a human may approve")

    ttl_hours = serializers.IntegerField(
        min_value=1, max_value=MAX_TTL_HOURS, default=48,
        help_text=f"How long this stays valid. At most {MAX_TTL_HOURS} hours.")

    allowed_categories = serializers.ListField(
        child=serializers.CharField(max_length=120, allow_blank=False),
        allow_empty=True, required=False, default=list, max_length=50,
        help_text="Empty means any category")

    def validate_domain(self, value):
        return value.strip().lower()

    def validate_allowed_categories(self, value):
        """De-duplicated case-insensitively, the buyer's casing kept on the
        survivor.

        This list becomes the signed `cats` claim, which engine.evaluate compares
        against the product's category with a plain `not in` -- so a duplicate is
        a claim that says the same thing twice. Casing is preserved because the
        claim is also what an auditor reads back.

        Blanks are refused rather than dropped, by allow_blank=False on the child
        above: "" would be a category no product can ever match, and naming that
        as a field error is more use to whoever sent it than silently shortening
        their list. The strip-and-skip below is what makes this function safe to
        read on its own; the child field is what actually reports the blank.
        """
        seen, out = set(), []
        for raw in value:
            category = raw.strip()
            if category and category.casefold() not in seen:
                seen.add(category.casefold())
                out.append(category)
        return out

    def validate(self, attrs):
        """The two relations between the caps, refused at the door with the field
        name on them.

        Duplicated in services.issue_mandate on purpose -- these are properties
        of a mandate, not of a request body, and the management command reaches
        the service without passing through here. What this adds is a 400 that
        names which box is wrong, instead of a message.
        """
        cap = _paise(attrs["per_txn_cap"])
        total = _paise(attrs["total_cap"])
        ceiling = _paise(attrs["step_up_ceiling"])

        if cap > total:
            raise serializers.ValidationError(
                {"per_txn_cap": "Cannot be more than the total budget."})
        if ceiling < cap:
            raise serializers.ValidationError(
                {"step_up_ceiling": "Cannot be below the per-purchase cap -- "
                                    "nothing would ever pass."})

        attrs["per_txn_cap_paise"] = cap
        attrs["total_cap_paise"] = total
        attrs["step_up_ceiling_paise"] = ceiling
        return attrs


class MandateSerializer(BaseSerializer):
    """One mandate as the buyer's list draws it.

    No token -- see MandateIssuedSerializer. Two explicit classes rather than one
    pruned with BaseSerializer(exclude=...): that __init__ touches self.fields
    BEFORE super().__init__() runs, and two instances of one class pruned
    differently collapse into a single drf-spectacular component unless each is
    given a component_name.
    """

    store = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    per_txn_cap = serializers.SerializerMethodField()
    total_cap = serializers.SerializerMethodField()
    step_up_ceiling = serializers.SerializerMethodField()
    spent = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()

    class Meta:
        model = Mandate
        fields = ("uuid", "jti", "kid", "store", "subject", "state", "currency",
                  "allowed_categories", "created_at", "not_before", "expires_at",
                  "revoked_at",
                  "per_txn_cap_paise", "total_cap_paise", "step_up_ceiling_paise",
                  "spent_paise", "remaining_paise",
                  "per_txn_cap", "total_cap", "step_up_ceiling", "spent", "remaining")
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_store(self, obj):
        """Domain and business name.

        Deliberately not the kill switch or the payment readiness: those are the
        merchant's operational state, they are reported once at issue time as
        warnings, and a buyer's mandate list is not a place to poll another
        party's configuration.
        """
        return {"domain": obj.tenant.domain, "business_name": obj.tenant.business_name}

    def get_state(self, obj) -> str:
        """revoked / expired / pending / live -- one word, derived once here.

        The precedence is the point. Revoked beats expired, because a buyer who
        revoked wants to see that they did even after the window closed, and the
        ledger entry for that revoke has to be explicable by the row. `pending`
        is not_before in the future, which Mandate.is_live folds in with expired
        -- fine for a boolean gate, wrong for a screen that has to say why.
        """
        now = self.context.get("now") or timezone.now()
        if obj.revoked_at is not None:
            return "revoked"
        if now >= obj.expires_at:
            return "expired"
        if now < obj.not_before:
            return "pending"
        return "live"

    def get_per_txn_cap(self, obj) -> float:
        return obj.per_txn_cap_paise / 100

    def get_total_cap(self, obj) -> float:
        return obj.total_cap_paise / 100

    def get_step_up_ceiling(self, obj) -> float:
        return obj.step_up_ceiling_paise / 100

    def get_spent(self, obj) -> float:
        return obj.spent_paise / 100

    def get_remaining(self, obj) -> float:
        return obj.remaining_paise / 100


class MandateIssuedSerializer(MandateSerializer):
    """The create and retrieve shape: the mandate plus the token itself.

    The token is a bearer credential -- whoever holds it can spend up to the caps
    -- so it is on these two responses and NOT on the list. That is a smaller
    distinction than PlatformAPIKey's, which stores only a hash and truly cannot
    re-display, and it is the right one here for a reason the model states: the
    JWS is kept "for audit and re-display", because the buyer's only way to hand
    it to a second agent is to read it back. Keeping it off the list keeps it out
    of the one response most likely to be cached, logged, or drawn on a screen
    with ten of them.
    """

    token = serializers.CharField(read_only=True)
    warnings = serializers.SerializerMethodField()

    class Meta(MandateSerializer.Meta):
        fields = MandateSerializer.Meta.fields + ("token", "warnings")
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_warnings(self, obj):
        """Facts about the store that will make this mandate refuse, reported
        once at issue rather than enforced as a refusal to issue.

        Issuing is the buyer's act; the kill switch and the Razorpay linkage are
        the merchant's, and they are checked at decision time by the engine and
        by checkout's MERCHANT_NOT_PAYMENT_READY. Refusing to issue against a
        store with agents switched off would make the buyer re-mint a mandate the
        moment the merchant switches back, and would turn this endpoint into an
        oracle for another party's settings. Telling them here is a bounded
        disclosure to somebody who learns the same fact from their very next
        checkout.
        """
        return self.context.get("warnings", [])
