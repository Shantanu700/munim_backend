from django.contrib import admin

from modules.twin.models import CatalogRead, Event, IngestJob, Product, WebhookHit
from modules.twin.orders import Order


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("title", "sku", "rupees", "currency", "in_stock", "is_visible_to_agents",
                    "confidence", "source", "fetched_at")
    list_filter = ("source", "in_stock", "is_visible_to_agents", "currency", "tenant")
    search_fields = ("title", "sku", "url")
    readonly_fields = ("raw",)

    @admin.display(description="price", ordering="price_paise")
    def rupees(self, obj):
        return f"{obj.price_paise / 100:,.2f}"


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "title", "sku", "rupees", "currency", "status",
                    "llm_client", "buyer", "authorized_at", "paid_at")
    list_filter = ("status", "llm_client", "currency", "tenant")
    search_fields = ("order_number", "title", "sku", "plink_id", "reference_id")
    readonly_fields = ("order_number", "uuid", "authorized_at", "ledger_seq",
                       "failed_attempts", "last_failure_at")

    @admin.display(description="amount", ordering="amount_paise")
    def rupees(self, obj):
        return obj.rupees


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "razorpay_event_id", "signature_valid", "tenant", "received_at")
    list_filter = ("event_type", "signature_valid", "tenant")
    readonly_fields = ("payload",)


@admin.register(WebhookHit)
class WebhookHitAdmin(admin.ModelAdmin):
    list_display = ("received_at", "outcome", "tenant", "merchant_domain",
                    "razorpay_event_id", "event_type", "signature_valid")
    list_filter = ("outcome", "signature_valid", "tenant")
    search_fields = ("razorpay_event_id", "merchant_domain", "event_type")


@admin.register(CatalogRead)
class CatalogReadAdmin(admin.ModelAdmin):
    list_display = ("day", "tenant", "agent_label", "tool", "count", "last_read_at")
    list_filter = ("tool", "agent_label", "tenant")
    date_hierarchy = "day"
    readonly_fields = ("tenant", "agent_label", "day", "tool", "count",
                       "last_read_at", "created_at", "uuid")


@admin.register(IngestJob)
class IngestJobAdmin(admin.ModelAdmin):
    list_display = ("uuid", "tenant", "created_by", "job_status", "processed", "total_urls",
                     "created_count", "updated_count", "skipped_count", "started_at", "finished_at")
    list_filter = ("job_status", "tenant")
    readonly_fields = ("error_message",)
