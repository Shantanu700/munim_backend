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
