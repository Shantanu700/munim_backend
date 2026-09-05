from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProductOut(BaseModel):
    url: str = Field(description="Canonical product page. Pass this to checkout.")
    sku: str
    title: str
    price: str = Field(description='Catalog price, e.g. "8500.00". Show this to a person.')
    price_paise: int = Field(description="The same price in paise. Use this for checkout.")
    currency: str
    in_stock: bool
    category: str = ""
    image_url: str = ""
    confidence: float = Field(
        description="How completely this row was recovered from the store's own markup, "
                    "0 to 1. Low-confidence rows may need a human before they can be sold."
    )
    catalog_fetched_at: datetime

    @classmethod
    def of(cls, product) -> "ProductOut":
        return cls(
            url=product.url,
            sku=product.sku,
            title=product.title,
            price=f"{product.price_paise / 100:.2f}",
            price_paise=product.price_paise,
            currency=product.currency,
            in_stock=product.in_stock,
            category=(product.raw or {}).get("category", "") or "",
            image_url=product.image_url,
            confidence=product.confidence,
            catalog_fetched_at=product.fetched_at,
        )


class SearchResult(BaseModel):
    merchant: str
    count: int
    products: list[ProductOut]


class MerchantOut(BaseModel):
    domain: str
    business_name: str = ""


class MerchantResult(BaseModel):
    count: int
    merchants: list[MerchantOut]


class MultiSearchResult(BaseModel):
    """Cross-merchant search result. One entry per merchant with any hits."""
    count: int = Field(description="Total products across all merchants returned")
    by_merchant: dict[str, list[ProductOut]]


class PaymentAttemptOut(BaseModel):
    """One payment link that was minted for an order."""
    attempt: int = Field(description="1 for the original link, 2 for the first retry, ...")
    status: str = Field(description="pending, paid, failed or expired.")
    amount: str = Field(description='What this link asked for, e.g. "8500.00".')
    amount_paise: int
    payment_url: str = Field("", description="The link itself. Only worth showing "
                                             "someone while status is pending.")
    requested_at: datetime
    expires_at: datetime = Field(description="After this, the link cannot be paid.")
    paid_at: datetime | None = None
    failure_reason_code: str = Field("", description="Why this attempt ended, if it did.")

    @classmethod
    def of(cls, payment) -> "PaymentAttemptOut":
        return cls(
            attempt=payment.attempt,
            status=payment.status,
            amount=f"{payment.amount_paise / 100:.2f}",
            amount_paise=payment.amount_paise,
            payment_url=payment.short_url,
            requested_at=payment.requested_at,
            expires_at=payment.expires_at,
            paid_at=payment.paid_at,
            failure_reason_code=payment.failure_reason_code,
        )


class OrderOut(BaseModel):
    """One order, as a buyer would want it summarised."""
    order_number: str = Field(description="The handle for this order. Pass it to "
                                          "get_payment_status or retry_payment.")
    status: str = Field(description="pending (a link is out), paid, failed or expired. "
                                    "Derived from the attempts, so an order stays paid "
                                    "even if an older link of its own later expires.")
    title: str
    sku: str = ""
    quantity: int = 1
    amount: str = Field(description='Order total, e.g. "8500.00".')
    amount_paise: int
    currency: str = "INR"
    attempts: int = Field(description="How many payment links this order has had.")
    ordered_at: datetime
    paid_at: datetime | None = None
    failure_reason_code: str = ""
    payment_url: str = Field("", description="The current link, if one is still live.")
    product_url: str = ""

    @classmethod
    def of(cls, order) -> "OrderOut":
        return cls(
            order_number=order.order_number,
            status=order.status,
            title=order.title,
            sku=order.sku,
            quantity=order.quantity,
            amount=f"{order.amount_paise / 100:.2f}",
            amount_paise=order.amount_paise,
            currency=order.currency,
            attempts=order.attempts,
            ordered_at=order.authorized_at,
            paid_at=order.paid_at,
            failure_reason_code=order.failure_reason_code,
            payment_url=order.short_url,
            product_url=order.product_url,
        )


class OrderListResult(BaseModel):
    merchant: str
    count: int
    orders: list[OrderOut]
    total_paid: str = Field(description='Sum of the PAID orders listed, e.g. "17000.00". '
                                        'Only the ones returned, not all time.')
    total_paid_paise: int = 0


class OrderDetailOut(BaseModel):
    """An order plus every payment link it has had."""
    order: OrderOut
    payments: list[PaymentAttemptOut]
    retryable: bool = Field(description="Whether retry_payment would be accepted right "
                                        "now. False when the order is paid, a link is "
                                        "still live, or it is out of attempts.")
    retry_blocked_reason: str = Field("", description="Why retryable is false.")


class CheckoutResult(BaseModel):
    verdict: str = Field(description="ALLOW, STEP_UP or DENY")
    reason_code: str
    message: str = Field(description="What happened, and what to do next.")
    payment_url: str = Field("", description="Present only on ALLOW. Give this to the buyer.")
    approval_url: str = Field("", description="Present only on STEP_UP. A human must open it.")
    amount_paise: int = 0
    live_price_paise: int | None = Field(
        None, description="The price read from the live page just now. On PRICE_CHANGED, "
                          "re-quote at this value."
    )
    sku: str = ""
    title: str = ""
    ledger_seq: int | None = Field(None, description="Audit ledger entry for this decision.")
