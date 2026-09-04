from rest_framework import serializers

from base.serializers import BaseSerializer
from modules.twin.models import Product, WebhookHit



class WebhookResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    duplicate = serializers.BooleanField(required=False)
    event_pk = serializers.IntegerField(required=False)


class WebhookHitSerializer(BaseSerializer):
    """One logged hit to the Razorpay webhook receiver, scoped to the caller's store."""

    outcome = serializers.CharField(source="get_outcome_display")

    class Meta:
        model = WebhookHit
        fields = ("uuid", "outcome", "razorpay_event_id", "event_type",
                  "signature_valid", "error_detail", "received_at")


WEBHOOK_HIT_LIST_FIELDS = WebhookHitSerializer.Meta.fields


def webhook_hit_list_serializer(*args, **kwargs):
    kwargs.setdefault("component_name", "WebhookHit")
    return WebhookHitSerializer(*args, fields=WEBHOOK_HIT_LIST_FIELDS, **kwargs)


class WebhookHitListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds the real paginated envelope by hand."""

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = webhook_hit_list_serializer(many=True)


class IngestRequestSerializer(serializers.Serializer):
    source = serializers.URLField()
    domain = serializers.CharField(required=False, allow_blank=True)
    business_name = serializers.CharField(required=False, allow_blank=True)
    match = serializers.CharField(required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(required=False, default=0, min_value=0)


class IngestResponseSerializer(serializers.Serializer):
    job = serializers.UUIDField()
    stream_url = serializers.CharField()
    status = serializers.CharField()
    webhook_url = serializers.CharField(allow_null=True)
    mcp_url = serializers.CharField(allow_null=True)


class ProductSerializer(BaseSerializer):
    """Everything the merchant sees about one product.

    Omits the `raw` blob a row was normalized from, which is an ingestion diagnostic
    rather than anything a merchant reads.
    """

    class Meta:
        model = Product
        fields = ("uuid", "url", "sku", "title", "price_paise", "currency", "in_stock",
                  "image_url", "is_visible_to_agents", "confidence", "source",
                  "fetched_at","in_stock")


PRODUCT_LIST_FIELDS = ("uuid", "sku", "title", "price_paise", "currency", "image_url", "in_stock")

PRODUCT_LIST_DESCRIPTION = (
    "One row of the catalogue table: what the table draws, plus the `uuid` that "
    "addresses the row for `GET /ingest/products/{uuid}` and its edit and delete. "
    "The SSE `product` event carries these same fields, so a client renders a streamed "
    "row and a listed row without telling them apart."
)


def product_list_serializer(*args, **kwargs):
    """ProductSerializer narrowed to the catalogue-table row.

    signals.py._on_product_saved must stay in step with PRODUCT_LIST_FIELDS -- it
    publishes these same keys plus a `type` discriminator.

    "Product" is what the class name would derive to anyway; it is passed explicitly so
    that this projection, not the unnarrowed shape, is the one holding that component.
    The name that actually has to be spelled out is the full shape's -- see the
    component_name on ProductDetailAV.get.
    """
    kwargs.setdefault("component_name", "Product")
    kwargs.setdefault("component_description", PRODUCT_LIST_DESCRIPTION)
    return ProductSerializer(*args, fields=PRODUCT_LIST_FIELDS, **kwargs)


class ProductWriteSerializer(BaseSerializer):
    """Fields a merchant may set directly, whether adding a product by hand or
    editing one that was ingested.

    Excludes `source`, `confidence`, `raw`, `fetched_at` -- those describe how a
    catalog row was found, and mean something different for a row a merchant
    typed in themselves.
    """

    class Meta:
        model = Product
        fields = ("url", "sku", "title", "price_paise", "currency",
                  "in_stock", "image_url", "is_visible_to_agents")


class ProductListResponseSerializer(serializers.Serializer):
    """Schema-only: the view builds the real paginated envelope by hand."""

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = product_list_serializer(many=True)


class IngestStateSerializer(serializers.Serializer):
    """Everything the catalogue screen needs on load, in one response.

    `job`/`stream_url`/`status` are null unless an ingestion is currently in flight;
    when they are not, the client can re-attach its EventSource to a run that started
    before this page load.
    """

    job = serializers.UUIDField(allow_null=True)
    stream_url = serializers.CharField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    domain = serializers.CharField(allow_blank=True)
    business_name = serializers.CharField(allow_blank=True)
    webhook_url = serializers.CharField(allow_null=True)
    mcp_url = serializers.CharField(allow_null=True)
