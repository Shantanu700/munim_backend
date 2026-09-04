from django.db import models


class RuleKind(models.TextChoices):
    """The rules a merchant can configure. Fixed in the backend, not invented
    by merchants -- each one needs a checker in registry.py and a place in the
    engine's sequence, neither of which a form can supply."""

    PER_ORDER_CEILING = "per_order_ceiling", "Per-order ceiling"
    DAILY_SPEND_CAP = "daily_spend_cap", "Daily agent spend cap"
    PRICE_CHANGE_TOLERANCE = "price_change_tolerance", "Price change tolerance"
    COMPLETE_PRODUCT_DATA = "complete_product_data", "Complete product data"
    BLOCKED_CATEGORIES = "blocked_categories", "Blocked categories"
    ALLOWED_CATEGORIES = "allowed_categories", "Allowed categories"
    AGENT_ALLOWLIST = "agent_allowlist", "Agent allowlist"
    MIN_CATALOG_CONFIDENCE = "min_catalog_confidence", "Minimum catalogue confidence"


class RuleState(models.TextChoices):
    """DRAFT and OFF are identical to the engine -- both mean "does not run".

    They are distinct so the screen can say *why* a rule is not running:
    DRAFT has never been turned on, OFF was turned on once and then turned
    off. Merchants read those two states very differently when deciding
    whether a refusal count of zero means the rule works or means nobody
    ever enabled it.

    Resist making DRAFT mean "evaluate but do not act". A shadow mode that
    records what it would have refused is a genuinely useful and completely
    separate feature; overloading DRAFT gives you a rule that is enforcing
    for analytics and not enforcing for money.
    """

    DRAFT = "draft", "Never enforced"
    ENFORCING = "enforcing", "Enforcing"
    OFF = "off", "Off"


class OnBreach(models.TextChoices):
    """What happens when a rule is breached. The merchant's choice, per rule.

    This is why a reason code no longer implies a verdict: the same
    STORE_TXN_CAP_EXCEEDED is a refusal for one store and an approval request
    for the next.
    """

    REFUSE = "refuse", "Refuse"
    ASK_BUYER = "ask_buyer", "Ask the buyer"
    ASK_MERCHANT = "ask_merchant", "Ask the merchant"
