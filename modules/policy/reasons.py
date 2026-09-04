"""Verdicts and reason codes.

Every money decision resolves to exactly one (verdict, reason_code) pair, and both
are written to the audit ledger -- denials included. A refusal you cannot produce
on demand is indistinguishable from a bug.

Codes stay under 64 characters to fit LedgerEntry.reason_code, and verdicts under
16 to fit LedgerEntry.decision.
"""
from django.db import models


class Verdict(models.TextChoices):
    ALLOW = "ALLOW", "Allow"
    STEP_UP = "STEP_UP", "Needs human approval"
    DENY = "DENY", "Deny"


class StepUpActor(models.TextChoices):
    """Who is asked to approve. The buyer owns their mandate; the merchant owns
    their store rules. Sending a store-policy breach to the buyer would ask the
    wrong person for permission they do not hold."""
    BUYER = "buyer", "Buyer"
    MERCHANT = "merchant", "Merchant"


class ReasonCode(models.TextChoices):
    MANDATE_MISSING = "MANDATE_MISSING"
    MANDATE_MALFORMED = "MANDATE_MALFORMED"
    MANDATE_BAD_SIGNATURE = "MANDATE_BAD_SIGNATURE"
    MANDATE_UNKNOWN_KEY = "MANDATE_UNKNOWN_KEY"
    MANDATE_ALG_UNSUPPORTED = "MANDATE_ALG_UNSUPPORTED"
    MANDATE_NOT_FOUND = "MANDATE_NOT_FOUND"

    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_NOT_YET_VALID = "MANDATE_NOT_YET_VALID"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_TENANT_MISMATCH = "MANDATE_TENANT_MISMATCH"

    STORE_AGENTS_DISABLED = "STORE_AGENTS_DISABLED"

    MANDATE_CATEGORY_NOT_ALLOWED = "MANDATE_CATEGORY_NOT_ALLOWED"
    STORE_CATEGORY_NOT_ALLOWED = "STORE_CATEGORY_NOT_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AGENT_NOT_ALLOWED = "AGENT_NOT_ALLOWED"

    MERCHANT_NOT_PAYMENT_READY = "MERCHANT_NOT_PAYMENT_READY"

    ITEM_NOT_IN_CATALOG = "ITEM_NOT_IN_CATALOG"
    LIVE_CHECK_FAILED = "LIVE_CHECK_FAILED"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PRICE_CHANGED = "PRICE_CHANGED"
    INCOMPLETE_PRODUCT_DATA = "INCOMPLETE_PRODUCT_DATA"

    MANDATE_TOTAL_EXCEEDED = "MANDATE_TOTAL_EXCEEDED"
    STEP_UP_CEILING_EXCEEDED = "STEP_UP_CEILING_EXCEEDED"

    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    STORE_TXN_CAP_EXCEEDED = "STORE_TXN_CAP_EXCEEDED"
    STORE_DAILY_CAP_EXCEEDED = "STORE_DAILY_CAP_EXCEEDED"
    LOW_CONFIDENCE_ROW = "LOW_CONFIDENCE_ROW"

    LINK_EXPIRED = "LINK_EXPIRED"
    LINK_CANCELLED = "LINK_CANCELLED"
    PAYMENT_ATTEMPT_FAILED = "PAYMENT_ATTEMPT_FAILED"

    ALLOWED = "ALLOWED"


DESCRIPTIONS: dict[str, str] = {
    ReasonCode.MANDATE_MISSING: "The caller sent no mandate at all, so there was nothing "
                                "authorising it to spend here.",
    ReasonCode.MANDATE_MALFORMED: "The mandate was not readable as a signed token.",
    ReasonCode.MANDATE_BAD_SIGNATURE: "The mandate's signature did not check out. Anyone can "
                                      "claim to be an agent; only a valid signature proves it.",
    ReasonCode.MANDATE_UNKNOWN_KEY: "The mandate was signed with a key this store does not know.",
    ReasonCode.MANDATE_ALG_UNSUPPORTED: "The mandate was signed with an algorithm we do not accept.",
    ReasonCode.MANDATE_NOT_FOUND: "The mandate verified, but no such mandate exists here.",
    ReasonCode.MANDATE_EXPIRED: "The mandate had expired before this purchase was attempted.",
    ReasonCode.MANDATE_NOT_YET_VALID: "The mandate's start time had not arrived yet.",
    ReasonCode.MANDATE_REVOKED: "The buyer had already revoked this mandate.",
    ReasonCode.MANDATE_TENANT_MISMATCH: "The mandate was issued for a different store.",
    ReasonCode.STORE_AGENTS_DISABLED: "Your kill switch was on. Every agent purchase refuses "
                                      "while it stays on.",
    ReasonCode.MANDATE_CATEGORY_NOT_ALLOWED: "The buyer's mandate does not cover this product's "
                                             "category.",
    ReasonCode.STORE_CATEGORY_NOT_ALLOWED: "Your rules do not let agents buy in this product's "
                                           "category.",
    ReasonCode.CURRENCY_MISMATCH: "The agent, the mandate and your store did not agree on a "
                                  "currency.",
    ReasonCode.AGENT_NOT_ALLOWED: "This agent is not on your allowed list.",
    ReasonCode.MERCHANT_NOT_PAYMENT_READY: "Your store cannot take payment yet, so nothing could "
                                           "be charged.",
    ReasonCode.ITEM_NOT_IN_CATALOG: "The agent asked for a page that is not a product we sell.",
    ReasonCode.LIVE_CHECK_FAILED: "We could not re-read the product page at the moment of "
                                  "purchase, so the price and stock could not be trusted.",
    ReasonCode.OUT_OF_STOCK: "The product page said out of stock when the purchase was attempted.",
    ReasonCode.PRICE_CHANGED: "The price the agent quoted no longer matched the live page. The "
                              "agent has to re-quote against the price we just read.",
    ReasonCode.INCOMPLETE_PRODUCT_DATA: "We do not hold enough about this product to sell it to "
                                        "an agent safely.",
    ReasonCode.MANDATE_TOTAL_EXCEEDED: "This purchase would have taken the buyer past the total "
                                       "their mandate allows. No one can wave this through.",
    ReasonCode.STEP_UP_CEILING_EXCEEDED: "The amount is above the ceiling the buyer signed, so it "
                                         "cannot even be put to a human.",
    ReasonCode.BUDGET_EXCEEDED: "The amount is over the buyer's own per-purchase cap, so the "
                                "buyer was asked to approve it.",
    ReasonCode.STORE_TXN_CAP_EXCEEDED: "The amount is over the per-purchase cap your rules set.",
    ReasonCode.STORE_DAILY_CAP_EXCEEDED: "This purchase would have taken today's agent spend past "
                                         "the daily cap your rules set.",
    ReasonCode.LOW_CONFIDENCE_ROW: "We are not confident enough in what we know about this "
                                   "product to let an agent buy it.",
    ReasonCode.LINK_EXPIRED: "The payment link ran out before anyone paid it, so the "
                             "order lapsed. Nothing was charged.",
    ReasonCode.LINK_CANCELLED: "The payment link was cancelled before it was paid, so the "
                               "order will not complete. Nothing was charged.",
    ReasonCode.PAYMENT_ATTEMPT_FAILED: "The buyer tried to pay and the payment was "
                                       "declined, then the link ran out. Worth a look -- "
                                       "they wanted this order.",
    ReasonCode.ALLOWED: "Every check passed and the purchase went through.",
}


def describe(reason_code: str) -> str:
    """The sentence for a code, or a blank -- an unknown code is not an error.

    Entries are permanent and codes are not: a row written by an older release
    can name a code this one has since retired, and the audit screen still has
    to render it.
    """
    return DESCRIPTIONS.get(reason_code, "")
