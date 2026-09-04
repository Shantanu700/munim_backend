"""Response shapes for the merchant's home dashboard.

Explicit serializers.Serializer throughout, matching order_serializers.py. The
views build every payload by hand, so these classes are the only thing the
OpenAPI schema has to describe them with.

Every money field is an IntegerField in paise and every share an IntegerField in
basis points. No FloatField and no DecimalField appear anywhere in this file, on
purpose -- see order_analytics.tiles on why a float average of currency is a
rounding bug waiting for a big enough month.
"""
from rest_framework import serializers

from modules.twin.api.v1.order_serializers import (
    MoneyCountSerializer,
    PaidThisMonthSerializer,
)
from modules.twin.api.v1.serializers import product_list_serializer
from modules.twin.dashboard_analytics import MAX_WINDOW_DAYS


class WindowSerializer(serializers.Serializer):
    """The window a card's numbers describe, echoed back beside them.

    Load-bearing rather than decoration: the payments and orders cards are
    all-time while the revenue and reads cards are windowed, so a client with no
    per-card window would have to hardcode which is which and would mislabel one
    the first time a default changed.
    """
    days = serializers.IntegerField()
    start = serializers.DateField(help_text="First day in the window, merchant-local.")
    end = serializers.DateField(help_text="Last day in the window, merchant-local.")



class SettlementSerializer(serializers.Serializer):
    """Where the money lands. See dashboard_analytics.settlement."""
    is_payment_ready = serializers.BooleanField()
    account_id = serializers.CharField(
        allow_null=True,
        help_text="Always null in practice: the Razorpay Partner accounts API that "
                  "would fill it is for sub-merchants, which this platform has none "
                  "of. Use key_id_masked as the settlement reference.")
    account_name = serializers.CharField(allow_null=True)
    key_id_masked = serializers.CharField(
        allow_null=True, help_text="rzp_live_...T2mLd. Masked because a dashboard "
                                   "gets screenshared.")
    key_mode = serializers.CharField(allow_null=True, help_text="live, test, or null.")
    business_name = serializers.CharField(allow_null=True)
    domain = serializers.CharField(allow_null=True)
    label = serializers.CharField(
        help_text="The sentence to print. Composed server-side so it cannot drift "
                  "from the numbers beside it.")


class PaymentsCardSerializer(serializers.Serializer):
    currency = serializers.CharField()
    all_time = MoneyCountSerializer()
    this_month = PaidThisMonthSerializer()
    prior_month = PaidThisMonthSerializer()
    direction = serializers.CharField(
        help_text="up, down, flat, or blank when there is no prior month.")
    settlement = SettlementSerializer()



class RevenuePointSerializer(serializers.Serializer):
    day = serializers.DateField()
    amount_paise = serializers.IntegerField()
    count = serializers.IntegerField()


class AgentBarSerializer(serializers.Serializer):
    agent = serializers.CharField(
        help_text="Also a valid ?agent= value on GET /orders/, so a bar can link "
                  "to the rows behind it. 'unknown' means no attribution.")
    amount_paise = serializers.IntegerField()
    count = serializers.IntegerField()
    share_bps = serializers.IntegerField(
        help_text="Basis points of the window's revenue. Divide by 100 for a "
                  "percentage.")


class PriorWindowSerializer(serializers.Serializer):
    days = serializers.IntegerField()
    amount_paise = serializers.IntegerField()
    count = serializers.IntegerField()
    average_basket_paise = serializers.IntegerField()


class RevenueCardSerializer(serializers.Serializer):
    window = WindowSerializer()
    currency = serializers.CharField()
    series = RevenuePointSerializer(
        many=True, help_text="Exactly `window.days` points, zero-filled, in order.")
    total = MoneyCountSerializer()
    agents = AgentBarSerializer(
        many=True, help_text="One entry per known agent, zero-filled. These sum to "
                             "`total`.")
    average_basket_paise = serializers.IntegerField()
    prior_window = PriorWindowSerializer(
        help_text="The same window immediately before this one -- the comparison "
                  "this system actually has the facts for.")
    storefront_comparison = serializers.IntegerField(
        allow_null=True,
        help_text="Always null. Munim only ever sees purchases agents made, so "
                  "there is no storefront average to compare a basket against. See "
                  "comparison_basis.")
    comparison_basis = serializers.CharField(
        help_text="'unavailable' today. Named rather than omitted so a client "
                  "cannot render a ratio from a missing field.")
    comparison_note = serializers.CharField()
    attribution_caveat = serializers.CharField()



class ReadPointSerializer(serializers.Serializer):
    day = serializers.DateField()
    count = serializers.IntegerField()


class ReadAgentSerializer(serializers.Serializer):
    agent = serializers.CharField()
    count = serializers.IntegerField()


class CatalogReadsCardSerializer(serializers.Serializer):
    """Reads of this store's catalogue by agents.

    list_merchants is deliberately absent from by_tool: it reads no store's
    catalogue, so there is no tenant it could be attributed to. See
    CatalogReadTool.
    """
    window = WindowSerializer()
    total = serializers.IntegerField()
    by_day = ReadPointSerializer(many=True)
    by_agent = ReadAgentSerializer(many=True)
    by_tool = serializers.DictField(child=serializers.IntegerField())



class ProductsCardSerializer(serializers.Serializer):
    visible = serializers.IntegerField(
        help_text="Agent-visible rows. The denominator for ready_bps.")
    discoverable = serializers.IntegerField(
        help_text="Visible and in stock: what a catalogue search returns.")
    complete = serializers.IntegerField(
        help_text="Visible and publishing every currently-required detail.")
    sellable = serializers.IntegerField(help_text="Discoverable AND complete.")
    required_fields = serializers.ListField(child=serializers.CharField())
    required_count = serializers.IntegerField(
        help_text="0 means the store requires no details, which is not the same as "
                  "having no rule -- see the policy card's `source`.")
    ready_bps = serializers.IntegerField(help_text="sellable / visible, in basis points.")
    missing_by_field = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Visible rows missing each detail. Counted over every known "
                  "detail, not only the required ones.")
    missing_histogram = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="How many rows are missing exactly N required details, keyed by N "
                  "as a string.")
    waiting_on_one_detail = serializers.IntegerField()
    caveat = serializers.CharField()


class ProductRowsSerializer(serializers.Serializer):
    """Schema-only: the ?missing= drilldown builds this envelope by hand,
    through DefaultPagination, the same way ProductListResponseSerializer does."""
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    missing = serializers.CharField(
        help_text="The detail these rows are missing, echoed back -- or 'any', "
                  "meaning each row is missing at least one known detail.")
    results = product_list_serializer(many=True)



class RuleValueSerializer(serializers.Serializer):
    """The four fields every policy value carries, and why none is optional.

    A cap printed without `enforcing` shows a limit that refuses nothing while
    the rule is a draft. A cap printed without `config_valid` shows a limit the
    engine is currently failing OPEN on, which is otherwise invisible to the
    merchant. `state` distinguishes the two not-enforcing states, which
    merchants read very differently -- see RuleState's docstring.
    """
    configured = serializers.BooleanField(help_text="False means there is no such rule row.")
    enforcing = serializers.BooleanField()
    state = serializers.CharField(help_text="draft, enforcing, off, or blank when absent.")
    config_valid = serializers.BooleanField()
    config_errors = serializers.DictField()


class CapSerializer(RuleValueSerializer):
    amount_paise = serializers.IntegerField(allow_null=True)
    unlimited = serializers.BooleanField(
        help_text="No limit. A blank cap means this; a cap of zero would refuse "
                  "every purchase in the store and is a different thing.")


class RequiredFieldsSerializer(RuleValueSerializer):
    fields = serializers.ListField(child=serializers.CharField())
    count = serializers.IntegerField()
    source = serializers.CharField(help_text="'configured' or 'default'.")
    requires_nothing = serializers.BooleanField(
        help_text="An explicit empty list. NOT the same as an unconfigured rule, "
                  "which falls back to the defaults.")


class CategoriesSerializer(RuleValueSerializer):
    categories = serializers.ListField(child=serializers.CharField())
    unrestricted = serializers.BooleanField(
        help_text="An empty list. For an allowlist this means every category is "
                  "allowed, not that nothing sells.")


class PolicyCardSerializer(serializers.Serializer):
    agent_traffic_enabled = serializers.BooleanField(
        help_text="The store-wide kill switch. Not a rule: it overrides every rule "
                  "and works when no policy is active.")
    currency = serializers.CharField()
    policy_name = serializers.CharField(allow_blank=True)
    has_active_policy = serializers.BooleanField(
        help_text="False means no store rules apply at all.")
    per_order_ceiling = CapSerializer()
    daily_spend_cap = CapSerializer()
    required_product_fields = RequiredFieldsSerializer()
    allowed_categories = CategoriesSerializer()
    blocked_categories = CategoriesSerializer(
        help_text="A separate rule from the allowlist on purpose: 'allow only "
                  "Dress' and 'block Dress' are different sentences.")



class MerchantHeaderSerializer(serializers.Serializer):
    domain = serializers.CharField(allow_null=True)
    business_name = serializers.CharField(allow_blank=True)
    currency = serializers.CharField()
    agent_traffic_enabled = serializers.BooleanField()


class DashboardSerializer(serializers.Serializer):
    """Every card, from one moment.

    `generated_at` plus a per-card `window` is what lets an all-time card and a
    14-day card sit on the same screen without either being mislabelled.
    """
    merchant = MerchantHeaderSerializer()
    generated_at = serializers.DateTimeField()
    payments = PaymentsCardSerializer()
    orders = serializers.DictField(
        help_text="As GET /orders/counts returns it: orders_total, by_status, tabs.")
    revenue = RevenueCardSerializer()
    catalog_reads = CatalogReadsCardSerializer()
    products = ProductsCardSerializer()
    policy = PolicyCardSerializer()


DAYS_HELP = (f"Window length in the merchant's own calendar days, including today. "
             f"Max {MAX_WINDOW_DAYS}. Out-of-range and unparseable values are "
             f"clamped or defaulted, never refused -- a window is a view "
             f"preference, not a claim that could be wrong.")
