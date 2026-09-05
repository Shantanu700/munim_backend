"""Payment projections for the MERCHANT's screen.

Separate from buyer_order_serializers.py's BuyerPaymentSerializer, which is the
same table seen by the other audience, and the difference is not cosmetic: this
one carries plink_id and reference_id -- the merchant's own Razorpay handles,
which are what they reconcile a disputed charge against -- and those are exactly
the fields buyer_order_serializers.py's docstring says the buyer must never be
shown. One class serving both would make that a one-word edit away from a leak.

tests/test_order_schema.py's NEVER_EXPOSED lists plink_id, and this module is
not in breach of it: that set is parametrised over the three ORDER components,
where it guards the buyer-reachable projection of a link. /orders/ already hands
merchants reference_id. The store's own handle for its own payment link, on a
screen behind IsMerchantPermission, is the one place it belongs.

One class per projection rather than one class narrowed three ways, following
buyer_order_serializers.py: drf-spectacular keys components on the serializer
CLASS, so distinct classes get distinct component names for free and the
collapse order_serializers.py works around with component_name= cannot arise.
"""
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.policy import reasons
from modules.twin.payments import Payment


class PaymentRowSerializer(BaseSerializer):
    """One payment attempt in the merchant's list.

    Carries flattened order context -- number, title, buyer -- because this list
    is read across orders, so a row that named only its attempt number would be
    unreadable without a second request per row.

    amount_paise is THIS link's amount and may differ from the order's: a retry
    re-runs the gate against the live page and the price it finds there may have
    moved (see Payment.amount_paise). The two are not interchangeable and the
    order's is deliberately not carried here.
    """

    order_uuid = serializers.UUIDField(source="order.uuid", read_only=True)
    order_number = serializers.CharField(source="order.order_number", read_only=True)
    order_title = serializers.CharField(source="order.title", read_only=True)
    buyer_label = serializers.SerializerMethodField()
    failure_reason_description = serializers.SerializerMethodField()
    is_live = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = (
            "uuid", "attempt", "status", "is_live",
            "amount_paise", "currency", "amount_paid_paise",
            "short_url", "plink_id", "reference_id",
            "requested_at", "expires_at", "paid_at", "closed_at",
            "failed_attempts", "last_failure_at",
            "failure_reason_code", "failure_reason_description",
            "order_uuid", "order_number", "order_title", "buyer_label",
        )

    def get_buyer_label(self, obj) -> str:
        """Who this was bought for.

        The mandate fallback is required rather than defensive, exactly as in
        OrderSerializer.get_buyer_label: Order.buyer is SET_NULL, so a deleted
        buyer must not blank the row.
        """
        order = obj.order
        if order.buyer_id:
            return order.buyer.display_name
        return order.mandate.subject if order.mandate_id else ""

    def get_failure_reason_description(self, obj) -> str:
        """Why this attempt failed, in the merchant's terms. Blank if it did not."""
        return reasons.describe(obj.failure_reason_code)

    def get_is_live(self, obj) -> bool:
        """Still payable: pending, and the link has not lapsed.

        Derived rather than stored, and computed by the model so this and the
        ?live= filter cannot drift apart. Note status alone does not answer it:
        a link stays `pending` until the reaper gets to it, so a lapsed one
        reads pending and live=false for as long as an hour.
        """
        return obj.is_live()


class PaymentDetailSerializer(PaymentRowSerializer):
    """One attempt, with a link to the checkout decision that authorised it.

    Order.ledger_seq holds the decision behind the order; this one holds the
    decision behind THIS retry, and the pair spans the whole story (see
    Payment.ledger_seq). Only on the detail view -- it is one more join's worth
    of reading than a table row wants.
    """

    decision_seq = serializers.IntegerField(source="ledger_seq", allow_null=True,
                                            read_only=True)
    decision_url = serializers.SerializerMethodField()

    class Meta(PaymentRowSerializer.Meta):
        fields = PaymentRowSerializer.Meta.fields + ("decision_seq", "decision_url")

    def get_decision_url(self, obj) -> str | None:
        """Where "see the decision behind this attempt" goes.

        Rendered from an integer with no join at all, as OrderSerializer does --
        the argument for ledger_seq being a BigIntegerField and not a FK into an
        append-only chain. Null on rows minted before retries carried one.
        """
        if obj.ledger_seq is None:
            return None
        return f"/audit/entries/{obj.ledger_seq}"


class PaymentListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds this envelope by hand.

    drf-spectacular cannot introspect base.pagination.KeysetPagination, which is
    not a DRF paginator -- the same reason OrderListResponseSerializer and
    BuyerOrderListResponseSerializer are both declared by hand.
    """

    results = PaymentRowSerializer(many=True)
    next_cursor = serializers.CharField(
        allow_null=True, help_text="Pass as ?cursor= for the next page.")
    has_more = serializers.BooleanField()
    total = serializers.IntegerField(
        allow_null=True,
        help_text="Only on a request that arrived without a cursor; null "
                  "thereafter, meaning unchanged since the first page.")
