"""Order projections for the BUYER's own screen.

Separate from order_serializers.py, which is the merchant's, because the two
audiences want different fields and one of them must never see the other's. A
merchant sees which agent claimed to place an order and the ledger entry behind
the decision; a buyer sees what they bought and whether it went through.

One class per projection rather than one class reused three ways. That is what
keeps drf-spectacular happy without any component_name overrides: it keys
components on the serializer CLASS, so distinct classes get distinct component
names for free, and the collision order_serializers.py has to work around --
three projections sharing OrderSerializer -- cannot arise here. (component_name
is a constructor kwarg on BaseSerializer, not a Meta option; putting it in Meta
looks right and does nothing.)

plink_id is absent deliberately and is listed in tests/test_order_schema.py's
NEVER_EXPOSED. It is the merchant's Razorpay-side handle, it is not needed to
pay anything -- short_url is the payable link -- and the buyer's screen is the
last place it should surface.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.policy import reasons
from modules.twin.orders import Order
from modules.twin.payments import Payment


class BuyerPaymentSerializer(BaseSerializer):
    """One payment link on a buyer's order."""

    failure_reason_description = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = (
            "attempt", "status", "amount_paise", "currency", "short_url",
            "requested_at", "expires_at", "paid_at",
            "failed_attempts", "last_failure_at",
            "failure_reason_code", "failure_reason_description",
        )

    def get_failure_reason_description(self, obj) -> str:
        return reasons.describe(obj.failure_reason_code)


class BuyerOrderSerializer(BaseSerializer):
    """One order in the buyer's list."""

    attempts = serializers.IntegerField(read_only=True)
    failure_reason_description = serializers.SerializerMethodField()
    merchant_domain = serializers.CharField(source="tenant.domain", read_only=True)

    class Meta:
        model = Order
        fields = (
            "uuid", "order_number", "merchant_domain", "title", "sku",
            "image_url", "product_url", "quantity", "unit_price_paise",
            "amount_paise", "currency", "status", "authorized_at", "paid_at",
            "short_url", "attempts", "failed_attempts", "last_failure_at",
            "failure_reason_code", "failure_reason_description",
        )

    def get_failure_reason_description(self, obj) -> str:
        return reasons.describe(obj.failure_reason_code)


class BuyerOrderListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds this envelope by hand.

    drf-spectacular cannot introspect base.pagination.KeysetPagination, which is
    not a DRF paginator -- the same reason OrderListResponseSerializer is
    declared by hand next door.

    Its own class rather than a shared one, for this module's usual reason:
    `results` is the buyer's projection, and the two audiences borrowing a
    component is exactly what must not happen.
    """

    results = BuyerOrderSerializer(many=True)
    next_cursor = serializers.CharField(
        allow_null=True, help_text="Pass as ?cursor= for the next page.")
    has_more = serializers.BooleanField()
    total = serializers.IntegerField(
        allow_null=True,
        help_text="Only on a request that arrived without a cursor; null "
                  "thereafter, meaning unchanged since the first page.")


class BuyerOrderDetailSerializer(BuyerOrderSerializer):
    """One order with every payment link it has had, oldest attempt first."""

    payments = serializers.SerializerMethodField()

    class Meta(BuyerOrderSerializer.Meta):
        fields = BuyerOrderSerializer.Meta.fields + ("payments",)

    @extend_schema_field(
        BuyerPaymentSerializer(many=True),
    )
    def get_payments(self, obj) -> list:
        return BuyerPaymentSerializer(obj.payments.order_by("attempt"), many=True).data
