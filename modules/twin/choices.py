from django.db import models


class IngestJobStatus(models.IntegerChoices):
    """PENDING and RUNNING are the two live states; the rest are terminal.

    CANCELLED is terminal but deliberately not FAILED: a merchant who stopped a
    crawl themselves should not be shown an error, and the counters the run
    reached before they stopped it are real -- the products it had already
    ingested stay in the catalogue. Everything that asks "is an ingestion in
    flight" filters on {PENDING, RUNNING}, so a cancelled job stops blocking the
    next POST /ingest the moment it is cancelled.
    """
    PENDING = 1, "Pending"
    RUNNING = 2, "Running"
    SUCCEEDED = 3, "Succeeded"
    FAILED = 4, "Failed"
    CANCELLED = 5, "Cancelled"


class WebhookHitOutcome(models.IntegerChoices):
    """Every distinct outcome RazorpayWebhookAV.post can reach, in the order
    the branches appear in that function -- see modules/twin/api/v1/views.py.
    """
    UNKNOWN_MERCHANT = 1, "Unknown merchant"
    MERCHANT_NOT_CONFIGURED = 2, "Merchant not configured"
    LEGACY_NOT_CONFIGURED = 3, "Legacy route not configured"
    INVALID_SIGNATURE = 4, "Invalid signature"
    MALFORMED_JSON = 5, "Malformed JSON"
    UNKNOWN_TENANT = 6, "Unknown tenant"
    PROCESSED = 7, "Processed"
    DUPLICATE = 8, "Duplicate"


class CatalogReadTool(models.TextChoices):
    """The MCP tools that count as "an agent looked at my catalogue".

    Deliberately excludes list_merchants. It reads no store's catalogue at all
    -- it answers "who is onboarded" -- so there is no tenant it could honestly
    be attributed to. Recording it against every merchant would inflate every
    store's read count with calls that never touched their products, which is a
    lie in the direction a merchant most wants to trust. Discovery is not a
    catalogue read; if platform-level discovery volume is ever wanted it belongs
    in its own untenanted table, not in this one. See agentmcp/server.py.
    """
    SEARCH_PRODUCTS = "search_products", "Search products"
    GET_PRODUCT = "get_product", "Get product"
    SEARCH_ALL_MERCHANTS = "search_all_merchants", "Search all merchants"
