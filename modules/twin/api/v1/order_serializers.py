"""Order projections for the merchant dashboard.

Kept out of twin/api/v1/serializers.py, which is the catalogue's.

Three projections over one class, each with an explicit component_name. That is
not decoration: drf-spectacular keys components on the serializer CLASS, so two
projections of one class silently collapse into whichever registered first.
tests/test_product_schema.py exists because of exactly that.
"""
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.policy import reasons
from modules.twin.orders import Order


class OrderSerializer(BaseSerializer):
    """Everything the merchant sees about one order.

    Includes who the agent claimed to be, the buyer it was placed for, and the
    audit entry holding the decision that allowed it.
    """

    buyer_label = serializers.SerializerMethodField()
    agent_label = serializers.SerializerMethodField()
    decision_seq = serializers.IntegerField(source="ledger_seq", allow_null=True,
                                            read_only=True)
    decision_url = serializers.SerializerMethodField()
    step_up_reason_code = serializers.SerializerMethodField()
    step_up_reason_description = serializers.SerializerMethodField()
    failure_reason_description = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "uuid", "order_number", "title", "sku", "amount_paise", "currency",
            "quantity", "llm_client", "status", "authorized_at", "paid_at",
            "product_url", "image_url", "unit_price_paise", "short_url",
            "reference_id", "failed_attempts", "last_failure_at",
            "buyer_label", "agent_label", "decision_seq", "decision_url",
            "step_up_reason_code", "step_up_reason_description",
            "failure_reason_code", "failure_reason_description",
        )

    def get_buyer_label(self, obj) -> str:
        """Who this was bought for.

        Order.buyer is SET_NULL, so the mandate fallback is required rather
        than defensive -- a deleted buyer must not blank the row.
        """
        if obj.buyer_id:
            return obj.buyer.display_name
        return obj.mandate.subject if obj.mandate_id else ""

    def get_agent_label(self, obj) -> str:
        """Which agent placed it, as the agent described ITSELF.

        Read from User-Agent and Origin at checkout (see agentmcp/clients.py),
        both of which are trivially spoofable. This is attribution for a
        dashboard, never an authenticated identity, and the UI has to label it
        that way rather than presenting it as fact.
        """
        return obj.llm_client or "unknown"

    def get_decision_url(self, obj) -> str | None:
        """Where "see the decision behind it" goes.

        Rendered from an integer with no join at all -- which is the whole
        argument for Order.ledger_seq being a BigIntegerField rather than a FK
        into an append-only chain.
        """
        if obj.ledger_seq is None:
            return None
        return f"/audit/entries/{obj.ledger_seq}"

    def get_step_up_reason_code(self, obj) -> str:
        """Why a human was asked, when one was.

        Blank for the ordinary case. An order exists only on ALLOW, so its own
        ledger entry's reason_code is always ALLOWED; the interesting code is
        the one on the step-up it came out of, if any.
        """
        return obj.step_up.reason_code if obj.step_up_id else ""

    def get_step_up_reason_description(self, obj) -> str:
        """That code as a sentence -- the treatment the audit table's _row()
        gives reason codes. An unknown code describes to a blank, not an error.
        """
        return reasons.describe(self.get_step_up_reason_code(obj))

    def get_failure_reason_description(self, obj) -> str:
        """Why a Needs attention row is in that state, in the merchant's terms.
        Blank for an order that has not failed or lapsed.
        """
        return reasons.describe(obj.failure_reason_code)


ORDER_ROW_FIELDS = ("uuid", "order_number", "title", "sku", "amount_paise", "currency",
                    "quantity", "llm_client", "agent_label", "status",
                    "authorized_at", "paid_at", "buyer_label", "decision_seq")

ORDER_ROW_DESCRIPTION = (
    "One row of the orders table: what the table draws, plus the `uuid` that "
    "addresses the row for `GET /orders/{uuid}` and the `decision_seq` that "
    "addresses the audit entry behind it. Timestamps are ISO-8601 -- relative "
    "rendering (\"Today 14:22\") belongs to the client, which knows the "
    "reader's timezone where the server only knows the merchant's."
)


def order_row_serializer(*args, **kwargs):
    """OrderSerializer narrowed to the orders-table row."""
    kwargs.setdefault("component_name", "OrderRow")
    kwargs.setdefault("component_description", ORDER_ROW_DESCRIPTION)
    return OrderSerializer(*args, fields=ORDER_ROW_FIELDS, **kwargs)


def order_export_serializer(*args, **kwargs):
    """OrderSerializer at full width, for the export.

    Not narrowed: an export is a report, and the row the merchant sees on
    screen is the row they should get in the file.
    """
    kwargs.setdefault("component_name", "OrderExportRow")
    kwargs.setdefault("component_description",
                      "One order in a JSON export, at the full detail width.")
    return OrderSerializer(*args, **kwargs)



class MoneyCountSerializer(serializers.Serializer):
    """A sum and how many rows made it. Money is always integer paise."""
    amount_paise = serializers.IntegerField()
    count = serializers.IntegerField()


class PaidThisMonthSerializer(MoneyCountSerializer):
    month = serializers.CharField(help_text="YYYY-MM, in the merchant's timezone.")


class AverageOrderValueSerializer(MoneyCountSerializer):
    prior_amount_paise = serializers.IntegerField()
    prior_month = serializers.CharField()
    direction = serializers.CharField(
        help_text="up, down, flat, or blank when there is no prior month.")


class OrderTilesSerializer(serializers.Serializer):
    """The three KPI tiles. Store-wide: they ignore the list's filters."""
    currency = serializers.CharField()
    paid_this_month = PaidThisMonthSerializer()
    awaiting_approval = MoneyCountSerializer(
        help_text="Step-ups still awaiting a human, NOT orders awaiting payment.")
    average_order_value = AverageOrderValueSerializer()


class OrderTabSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    count = serializers.IntegerField()
    source = serializers.CharField(
        help_text="'orders' means key is a valid ?status= value. 'step_ups' means "
                  "it is not -- those rows live behind the approvals surface.")


class OrderCountsSerializer(serializers.Serializer):
    """Tab counts. These deliberately do not sum to orders_total -- see the
    comment on order_analytics.counts()."""
    orders_total = serializers.IntegerField()
    by_status = serializers.DictField(child=serializers.IntegerField())
    tabs = OrderTabSerializer(many=True)


class OrderListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds this envelope by hand.

    drf-spectacular cannot introspect base.pagination.KeysetPagination, which
    is not a DRF paginator -- same reason LedgerListResponseSerializer is
    declared by hand.
    """
    results = order_row_serializer(many=True)
    next_cursor = serializers.CharField(allow_null=True,
                                        help_text="Pass as ?cursor= for the next page.")
    has_more = serializers.BooleanField()
    total = serializers.IntegerField(
        allow_null=True,
        help_text="Only on a request that arrived without a cursor; null "
                  "thereafter, meaning unchanged since the first page.")


class OrderExportFiltersSerializer(serializers.Serializer):
    status = serializers.CharField(allow_null=True)
    agent = serializers.CharField(allow_null=True)
    q = serializers.CharField(allow_null=True)


class OrderExportSerializer(serializers.Serializer):
    """Schema-only: the view builds this body by hand."""
    merchant = serializers.CharField(allow_null=True)
    exported_at = serializers.DateTimeField()
    filters = OrderExportFiltersSerializer()
    count = serializers.IntegerField()
    truncated = serializers.BooleanField(
        help_text="True when the row ceiling was hit; narrow the filters.")
    orders = order_export_serializer(many=True)
