from collections.abc import Callable

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.core.models import MasterDropdown, MerchantCategory
from modules.policy.choices import OnBreach, RuleKind, RuleState
from modules.policy.models import PolicyRule, StepUp, StorePolicy
from modules.policy.registry import (
    MAX_TOLERANCE_BPS,
    PRODUCT_DATA_FIELDS,
    REGISTRY,
    all_specs,
)



class StepUpSerializer(BaseSerializer):
    rupees = serializers.SerializerMethodField()
    cap_rupees = serializers.SerializerMethodField()
    store_cap_rupees = serializers.SerializerMethodField()
    quoted_rupees = serializers.SerializerMethodField()
    live_rupees = serializers.SerializerMethodField()
    drift_pct = serializers.SerializerMethodField()

    class Meta:
        model = StepUp
        fields = "__all__"

    def get_rupees(self, obj) -> float:
        return obj.amount_paise / 100

    def get_cap_rupees(self, obj) -> float:
        return (obj.detail.get("mandate_cap_paise") or 0) / 100

    def get_store_cap_rupees(self, obj) -> float:
        """The store limit that was breached.

        Reads `limit_paise` off the deciding rule. It used to read a
        `store_cap_paise` the engine put on every decision whether a store cap
        was involved or not; now only the rule that actually bound reports its
        own number, so this is 0 when the step-up was not about a store cap.
        """
        return (obj.detail.get("limit_paise") or 0) / 100

    def get_quoted_rupees(self, obj) -> float:
        return (obj.detail.get("quoted_paise") or obj.quoted_price_paise or 0) / 100

    def get_live_rupees(self, obj) -> float:
        return (obj.detail.get("live_paise") or obj.detail.get("live_price_paise") or 0) / 100

    def get_drift_pct(self, obj) -> float:
        return round((obj.detail.get("drift_bps") or 0) / 100, 2)


class MessageSerializer(serializers.Serializer):
    """The {"msg": ...} shape every non-resource response in this codebase
    uses, named so it appears in the schema instead of an untyped 200."""

    msg = serializers.CharField()


class StepUpDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "decline"])


class CheckoutOutcomeSerializer(serializers.Serializer):
    verdict = serializers.CharField()
    reason_code = serializers.CharField()
    message = serializers.CharField()
    payment_url = serializers.CharField(required=False, allow_blank=True)
    approval_url = serializers.CharField(required=False, allow_blank=True)
    amount_paise = serializers.IntegerField(required=False)
    live_price_paise = serializers.IntegerField(required=False, allow_null=True)
    ledger_seq = serializers.IntegerField(required=False, allow_null=True)
    sku = serializers.CharField(required=False, allow_blank=True)
    title = serializers.CharField(required=False, allow_blank=True)


class StepUpOutcomeSerializer(serializers.Serializer):
    step_up = StepUpSerializer()
    outcome = CheckoutOutcomeSerializer()




class _MoneyConfig(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1, allow_null=True, required=False)


class _CategoriesConfig(serializers.Serializer):
    categories = serializers.ListField(
        child=serializers.CharField(max_length=120, allow_blank=False),
        allow_empty=True, required=False, max_length=200,
    )


class _AgentsConfig(serializers.Serializer):
    agents = serializers.ListField(
        child=serializers.CharField(max_length=120, allow_blank=False),
        allow_empty=True, required=False, max_length=100,
    )


class _ProductDataConfig(serializers.Serializer):
    required_fields = serializers.ListField(
        child=serializers.ChoiceField(choices=list(PRODUCT_DATA_FIELDS)),
        allow_empty=True, required=False,
    )


class _ToleranceConfig(serializers.Serializer):
    tolerance_bps = serializers.IntegerField(min_value=0, max_value=MAX_TOLERANCE_BPS,
                                             required=False)


class _ConfidenceConfig(serializers.Serializer):
    min_confidence = serializers.FloatField(min_value=0.0, max_value=1.0, required=False)



AGENT_DROPDOWN_LABEL = "AI Agents"


def agent_options(tenant=None) -> list[str]:
    """The agent names a merchant may allowlist.

    Platform-wide and not per store, which is why the tenant is ignored: the
    `sub` claim an agent presents is minted by whoever issued the mandate, so
    the set of names is not a store's to extend. Ordered by id to keep the
    order the fixture seeded, the same as the picker MasterDropdownAV serves --
    unordered, the same dropdown renders differently on each call.
    """
    return list(
        MasterDropdown.objects.filter(parent__label__iexact=AGENT_DROPDOWN_LABEL)
        .order_by("id").values_list("label", flat=True)
    )


def category_options(tenant) -> list[str]:
    """One store's own categories, as its own dropdowns render them.

    `objects` and not `unfiltered_objects`: a category the merchant deleted
    must not be offered back to them, for the reason MerchantCategoryListAV
    spells out. Empty until the first crawl has run, and that is exactly why
    membership is only enforced when the list has something in it -- see
    check_options.
    """
    if tenant is None:
        return []
    return list(
        MerchantCategory.objects.filter(tenant=tenant)
        .order_by("name").values_list("name", flat=True)
    )


OPTION_SOURCES: dict[str, tuple[str, Callable[..., list[str]]]] = {
    RuleKind.AGENT_ALLOWLIST: ("agents", agent_options),
    RuleKind.BLOCKED_CATEGORIES: ("categories", category_options),
    RuleKind.ALLOWED_CATEGORIES: ("categories", category_options),
}


def check_options(kind, config: dict, tenant) -> None:
    """Refuse a config naming an agent or a category that does not exist, and
    canonicalise the ones that do. Mutates `config`.

    Deliberately not a ChoiceField inside the config serializers above.
    RuleSerializer re-validates stored config on every read to report
    `config_valid`, and a category the merchant deleted *after* writing the
    rule must not make a working rule read as broken -- the rule still
    evaluates, it simply stops matching anything. So membership is checked
    where a value is written, and nowhere else.

    An empty option list waives the check rather than refusing everything. A
    merchant configuring category rules before their catalogue has been crawled
    would otherwise be unable to name any category at all, and a deployment
    where the AI Agents fixture has not been loaded would refuse every agent
    name -- an empty picker must not become an unusable rule.

    Matched casefolded, like the checkers in registry.py compare, and the
    canonical row label is what gets stored -- so the form re-renders the option
    the merchant picked rather than whatever casing they happened to send.
    """
    key, options_for = OPTION_SOURCES.get(kind, (None, None))
    if key is None or key not in config:
        return
    options = options_for(tenant)
    if not options:
        return

    canonical = {option.casefold(): option for option in options}
    unknown, cleaned = [], []
    for value in config[key]:
        match = canonical.get(str(value).strip().casefold())
        if match is None:
            unknown.append(value)
        elif match not in cleaned:
            cleaned.append(match)
    if unknown:
        raise serializers.ValidationError({key: [
            f"{value!r} is not one of: {', '.join(options)}." for value in unknown]})
    config[key] = cleaned


CONFIG_SERIALIZERS = {
    RuleKind.PER_ORDER_CEILING: _MoneyConfig,
    RuleKind.DAILY_SPEND_CAP: _MoneyConfig,
    RuleKind.BLOCKED_CATEGORIES: _CategoriesConfig,
    RuleKind.ALLOWED_CATEGORIES: _CategoriesConfig,
    RuleKind.AGENT_ALLOWLIST: _AgentsConfig,
    RuleKind.COMPLETE_PRODUCT_DATA: _ProductDataConfig,
    RuleKind.PRICE_CHANGE_TOLERANCE: _ToleranceConfig,
    RuleKind.MIN_CATALOG_CONFIDENCE: _ConfidenceConfig,
}


def config_fields(kind, tenant=None) -> list[dict]:
    """One rule's config shape, for the editor panel to render generically.

    The frontend builds the form from this rather than hardcoding a panel per
    kind, so adding a rule to the registry does not need a frontend release.

    `tenant` is what makes the category dropdowns per-store. Without one the
    category rules report no options, which the form must read as "free text"
    rather than "no categories are selectable" -- the same thing it sees for a
    store whose catalogue has not been crawled yet, and the same thing
    check_options permits on write.
    """
    option_key, options_for = OPTION_SOURCES.get(kind, (None, None))
    serializer = CONFIG_SERIALIZERS[kind]()
    out = []
    for name, field in serializer.fields.items():
        spec = {
            "name": name,
            "type": type(field).__name__.replace("Field", "").lower(),
            "required": field.required,
            "allow_null": field.allow_null,
        }
        if isinstance(field, serializers.ListField):
            child = field.child
            spec["item_type"] = type(child).__name__.replace("Field", "").lower()
            spec["choices"] = list(getattr(child, "choices", {}) or []) or None
            if name == option_key:
                spec["choices"] = options_for(tenant) or None
        for bound in ("min_value", "max_value"):
            if getattr(field, bound, None) is not None:
                spec[bound] = getattr(field, bound)
        out.append(spec)
    return out



class RuleSerializer(BaseSerializer):
    """One rule row, plus the registry copy the screen prints beside it."""

    label = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    reason_code = serializers.SerializerMethodField()
    config_fields = serializers.SerializerMethodField()
    refused_30d = serializers.SerializerMethodField()
    held_back_paise_30d = serializers.SerializerMethodField()
    config_valid = serializers.SerializerMethodField()
    config_errors = serializers.SerializerMethodField()

    class Meta:
        model = PolicyRule
        fields = ("uuid", "kind", "config", "state", "on_breach", "created_at",
                  "first_enforced_at", "label", "description", "reason_code",
                  "config_fields", "refused_30d", "held_back_paise_30d",
                  "config_valid", "config_errors")

    def get_label(self, obj) -> str:
        return REGISTRY[obj.kind].label

    def get_description(self, obj) -> str:
        return REGISTRY[obj.kind].description

    def get_reason_code(self, obj) -> str:
        """What an agent is told when this rule refuses. Derived from the kind
        and never editable -- it is a wire contract, not a preference."""
        return REGISTRY[obj.kind].reason_code

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_config_fields(self, obj):
        return config_fields(obj.kind, self.context.get("tenant"))

    def _counts(self, obj) -> dict:
        return (self.context.get("counts") or {}).get(obj.kind) or {}

    def get_refused_30d(self, obj) -> int:
        return self._counts(obj).get("refused", 0)

    def get_held_back_paise_30d(self, obj) -> int:
        return self._counts(obj).get("held_back_paise", 0)

    def _validated(self, obj):
        body = CONFIG_SERIALIZERS[obj.kind](data=obj.config or {})
        return body, body.is_valid()

    def get_config_valid(self, obj) -> bool:
        """Whether this rule can actually be evaluated as stored.

        The engine fails a rule OPEN when its config raises -- right for the
        money path, but it means a rule can read as enforcing while refusing
        nothing, and the only trace is a server log the merchant cannot see.
        Config is validated on write, so a False here means a row that predates
        the current schema or was changed outside the API. Reported rather than
        appended to the ledger: the engine must not write while deciding, and
        one entry per misconfigured rule per checkout would flood the chain.
        """
        return self._validated(obj)[1]

    @extend_schema_field(serializers.DictField())
    def get_config_errors(self, obj):
        body, ok = self._validated(obj)
        return {} if ok else body.errors


class RuleWriteSerializer(serializers.Serializer):
    """Partial by design: flipping a toggle must not require the client to echo
    back a config it would then be able to clobber."""

    config = serializers.DictField(required=False)
    state = serializers.ChoiceField(choices=RuleState.choices, required=False)
    on_breach = serializers.ChoiceField(choices=OnBreach.choices, required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                {"msg": "Send at least one of config, state or on_breach."})
        if "config" in attrs:
            kind = self.context["kind"]
            body = CONFIG_SERIALIZERS[kind](data=attrs["config"])
            body.is_valid(raise_exception=True)
            config = {**REGISTRY[kind].default_config, **body.validated_data}
            check_options(kind, config, self.context.get("tenant"))
            attrs["config"] = config
        return attrs


class PolicySerializer(BaseSerializer):
    """A policy without its rules: what the list column draws."""

    rule_counts = serializers.SerializerMethodField()

    class Meta:
        model = StorePolicy
        fields = ("uuid", "name", "is_active", "created_at", "updated_at", "rule_counts")

    @extend_schema_field(serializers.DictField(child=serializers.IntegerField()))
    def get_rule_counts(self, obj):
        """all / enforcing / draft / off -- the filter chips above the table."""
        rules = list(obj.rules.all())
        return {
            "all": len(rules),
            RuleState.ENFORCING: sum(r.state == RuleState.ENFORCING for r in rules),
            RuleState.DRAFT: sum(r.state == RuleState.DRAFT for r in rules),
            RuleState.OFF: sum(r.state == RuleState.OFF for r in rules),
        }


class PolicyDetailSerializer(PolicySerializer):
    rules = serializers.SerializerMethodField()

    class Meta(PolicySerializer.Meta):
        fields = PolicySerializer.Meta.fields + ("rules",)

    @extend_schema_field(RuleSerializer(many=True))
    def get_rules(self, obj):
        """In registry order, not by primary key: the screen lists rules in a
        fixed order, and that order is also the tiebreak the engine uses when
        two rules are breached at once."""
        by_kind = {r.kind: r for r in obj.rules.all()}
        ordered = [by_kind[s.kind] for s in all_specs() if s.kind in by_kind]
        return RuleSerializer(ordered, many=True, context=self.context).data


class PolicyWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, allow_blank=False, trim_whitespace=True)


class KillSwitchSerializer(serializers.Serializer):
    agent_traffic_enabled = serializers.BooleanField()


class RuleCatalogEntrySerializer(serializers.Serializer):
    """The registry, so the editor can render a rule it has never seen."""

    kind = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField()
    reason_code = serializers.CharField()
    default_on_breach = serializers.CharField()
    default_config = serializers.DictField()
    config_fields = serializers.ListField(child=serializers.DictField())


class TilesSerializer(serializers.Serializer):
    """The four numbers across the top of the screen."""

    rules_enforcing = serializers.IntegerField()
    rules_defined = serializers.IntegerField()
    refused_30d = serializers.IntegerField()
    held_back_paise_30d = serializers.IntegerField()
    step_ups_30d = serializers.IntegerField()
    step_ups_approved_30d = serializers.IntegerField()
    step_ups_expired_30d = serializers.IntegerField()
    last_change_at = serializers.DateTimeField(allow_null=True)
    last_change_summary = serializers.CharField(allow_blank=True)


class OverviewSerializer(serializers.Serializer):
    """Everything the policy screen needs on load, in one response.

    One call rather than five: the tiles, the chips and the table all describe
    the same moment, and assembling them from separate requests lets the screen
    render a total that disagrees with the rows beneath it.
    """

    agent_traffic_enabled = serializers.BooleanField()
    currency = serializers.CharField()
    active_policy = serializers.CharField(allow_null=True)
    policies = PolicySerializer(many=True)
    tiles = TilesSerializer()


class HistoryEntrySerializer(serializers.Serializer):
    seq = serializers.IntegerField()
    at = serializers.DateTimeField()
    reason_code = serializers.CharField()
    actor_name = serializers.CharField(allow_blank=True)
    actor_email = serializers.CharField(allow_blank=True)
    detail = serializers.DictField()


class HistoryResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds the paginated envelope by hand."""

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = HistoryEntrySerializer(many=True)
