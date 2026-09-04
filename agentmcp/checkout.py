"""Checkout: the only path where money can move.

Sequence is deliberate. Re-read the live page first, gate on the re-read, and
only then create a Payment Link on the MERCHANT's Razorpay account. The catalog
is a snapshot taken at ingest; trusting it at commit time is exactly the failure
this step exists to prevent.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from agentmcp import catalog
from modules.ledger.models import LedgerEntry
from modules.policy import engine, jws
from modules.policy.engine import CheckoutRequest, LiveFacts, evaluate_and_record
from modules.policy.models import Mandate, StepUp
from modules.policy.reasons import ReasonCode, Verdict
from modules.twin.ingest import fetch, product_from_html
from modules.twin.orders import create_order
from payments.razorpay import PaymentError, create_payment_link

STEP_UP_MINUTES = 30
LINK_MINUTES = 30


class CheckoutOutcome:
    """What the tool reports back. Plain object, built by the paths below."""

    def __init__(self, verdict, reason_code, message, *, payment_url="", approval_url="",
                 amount_paise=0, live_price_paise=None, ledger_seq=None, sku="", title=""):
        self.verdict = verdict
        self.reason_code = reason_code
        self.message = message
        self.payment_url = payment_url
        self.approval_url = approval_url
        self.amount_paise = amount_paise
        self.live_price_paise = live_price_paise
        self.ledger_seq = ledger_seq
        self.sku = sku
        self.title = title


def read_live(url: str) -> LiveFacts | None:
    """Re-read price and stock from the merchant's own page, right now.

    Returns None when the page cannot be fetched or carries no usable Product
    markup. Callers must FAIL CLOSED on None -- falling back to the catalog
    price would defeat the point of re-reading.
    """
    html = fetch(url)
    if not html:
        return None
    item = product_from_html(html, url)
    if item is None or item.price_paise is None:
        return None
    return LiveFacts(price_paise=item.price_paise,
                     in_stock=bool(item.in_stock) if item.in_stock is not None else True,
                     fetched_at=timezone.now())


def _link_for(tenant, mandate, product, amount_paise, quantity, decision):
    """Create the Payment Link and record it. Raises PaymentError upward.

    Returns (body, reference_id). The reference travels back explicitly rather
    than being read out of the response, because it is the handle a
    payment.failed event resolves the order by -- and a handle that depends on
    the provider echoing a field back is a handle that can go missing.
    """
    reference_id = str(uuid.uuid4())         
    body = create_payment_link(
        tenant,
        amount_paise=amount_paise,
        description=f"{product.title} x{quantity} ({product.sku or 'no sku'})",
        reference_id=reference_id,
        notes={
            "munim_tenant": tenant.domain,
            "munim_mandate": str(mandate.jti),
            "munim_sku": product.sku or "",
            "munim_url": product.url[:200],
            "munim_ref": reference_id,
        },
        expire_minutes=LINK_MINUTES,
    )
    LedgerEntry.append(
        tenant=tenant, action="payment_link_created", decision=Verdict.ALLOW,
        reason_code=ReasonCode.ALLOWED,
        detail={"plink_id": body.get("id", ""), "reference_id": reference_id,
                "amount_paise": amount_paise, "short_url": body.get("short_url", ""),
                "mandate_jti": str(mandate.jti)},
    )
    return body, reference_id


def _step_up_because(decision, live) -> str:
    """Why a human is being asked, in a clause the agent can relay.

    Was a hardcoded "Over the limit", which was true when the only store rules
    were caps. A store rule can now step up on a category, a thin catalogue
    row, or a price that drifted, and telling the agent it was over a limit
    sends it off to re-quote against a limit that is not the problem.
    """
    return {
        ReasonCode.STORE_TXN_CAP_EXCEEDED: "Over this store's per-order limit",
        ReasonCode.STORE_DAILY_CAP_EXCEEDED: "Over this store's limit for today",
        ReasonCode.BUDGET_EXCEEDED: "Over the mandate's per-purchase limit",
        ReasonCode.LOW_CONFIDENCE_ROW: "This catalogue row needs checking first",
        ReasonCode.STORE_CATEGORY_NOT_ALLOWED: "This store restricts that category",
        ReasonCode.INCOMPLETE_PRODUCT_DATA: "That catalogue row is incomplete",
        ReasonCode.PRICE_CHANGED:
            f"The price rose to {live.price_paise / 100:,.2f} after the quote",
    }.get(decision.reason_code, f"Held for {decision.reason_code}")


def quote_and_gate(tenant, *, mandate_token: str, sku: str | None = None,
                   url: str | None = None, quantity: int = 1,
                   quoted_price_paise: int | None = None,
                   approved_reason: str | None = None,
                   step_up: StepUp | None = None,
                   llm_client: str = "", llm_client_raw: str = "") -> CheckoutOutcome:
    quantity = max(1, int(quantity or 1))
    product = catalog.get(tenant, sku=sku, url=url)          
    quote_from_caller = quoted_price_paise is not None
    quoted = int(quoted_price_paise if quote_from_caller else product.price_paise)

    facts = engine.facts_for(tenant)

    live = read_live(product.url)
    if live is None:
        _, entry = evaluate_and_record(
            CheckoutRequest(tenant=tenant, mandate_token=mandate_token,
                            product_url=product.url, quoted_price_paise=quoted,
                            quantity=quantity, quote_from_caller=quote_from_caller),
            product,
            LiveFacts(price_paise=-1, in_stock=False, fetched_at=timezone.now()),
            facts,
        )
        LedgerEntry.append(tenant=tenant, action="checkout", decision=Verdict.DENY,
                           reason_code=ReasonCode.LIVE_CHECK_FAILED,
                           detail={"url": product.url})
        return CheckoutOutcome(
            Verdict.DENY, ReasonCode.LIVE_CHECK_FAILED,
            "Could not re-read the product page, so the price and stock are unconfirmed. "
            "Nothing was charged. Try again shortly.",
            sku=product.sku, title=product.title, ledger_seq=entry.seq)

    request = CheckoutRequest(tenant=tenant, mandate_token=mandate_token,
                              product_url=product.url, quoted_price_paise=quoted,
                              quantity=quantity, quote_from_caller=quote_from_caller)
    decision, entry = evaluate_and_record(request, product, live, facts,
                                          approved_reason=approved_reason)
    amount = live.price_paise * quantity

    if decision.verdict == Verdict.DENY:
        msg = {
            ReasonCode.PRICE_CHANGED:
                f"The price changed to {live.price_paise / 100:,.2f} since the catalog was "
                f"built. Call checkout again with quoted_price_paise={live.price_paise}.",
            ReasonCode.OUT_OF_STOCK: "That item is out of stock right now.",
            ReasonCode.AGENT_NOT_ALLOWED:
                "This store accepts agent purchases only from named agents, and this "
                "mandate is not issued to one of them. Nothing was charged.",
            ReasonCode.STORE_CATEGORY_NOT_ALLOWED:
                "This store does not sell that category to agents.",
            ReasonCode.INCOMPLETE_PRODUCT_DATA:
                "That catalogue row is missing fields this store requires before it will "
                "sell to an agent. Nothing was charged.",
            ReasonCode.STORE_DAILY_CAP_EXCEEDED:
                "This store has reached its agent spending limit for today. Try tomorrow.",
        }.get(decision.reason_code, f"Refused: {decision.reason_code}.")
        return CheckoutOutcome(decision.verdict, decision.reason_code, msg,
                               amount_paise=amount, live_price_paise=live.price_paise,
                               ledger_seq=entry.seq, sku=product.sku, title=product.title)

    mandate = Mandate.objects.filter(jti=jws.verify(mandate_token).get("jti")).first()

    if decision.verdict == Verdict.STEP_UP:
        pending = StepUp.objects.create(
            tenant=tenant, mandate=mandate, actor=decision.step_up_actor,
            product_url=product.url, title=product.title, sku=product.sku,
            quantity=quantity, quoted_price_paise=quoted, amount_paise=amount,
            reason_code=decision.reason_code, detail=decision.detail,
            expires_at=timezone.now() + timedelta(minutes=STEP_UP_MINUTES),
        )
        who = "the buyer" if decision.step_up_actor == "buyer" else "the merchant"
        return CheckoutOutcome(
            decision.verdict, decision.reason_code,
            f"{_step_up_because(decision, live)}, so {who} has to approve this one. Send "
            f"them the approval link; it expires in {STEP_UP_MINUTES} minutes.",
            approval_url=f"/step-up/{pending.token}", amount_paise=amount,
            live_price_paise=live.price_paise, ledger_seq=entry.seq,
            sku=product.sku, title=product.title)

    if not tenant.is_payment_ready:
        LedgerEntry.append(tenant=tenant, action="checkout", decision=Verdict.DENY,
                           reason_code=ReasonCode.MERCHANT_NOT_PAYMENT_READY, detail={})
        return CheckoutOutcome(
            Verdict.DENY, ReasonCode.MERCHANT_NOT_PAYMENT_READY,
            f"{tenant.domain} has not connected a Razorpay account, so there is nowhere "
            f"to send the payment.", sku=product.sku, title=product.title)

    try:
        body, reference_id = _link_for(tenant, mandate, product, amount, quantity,
                                       decision)
    except PaymentError as exc:
        LedgerEntry.append(tenant=tenant, action="payment_link_failed", decision=Verdict.DENY,
                           reason_code="PAYMENT_LINK_FAILED",
                           detail={"error": str(exc), "status": exc.status})
        return CheckoutOutcome(Verdict.DENY, "PAYMENT_LINK_FAILED", str(exc),
                               amount_paise=amount, sku=product.sku, title=product.title)

    create_order(
        tenant=tenant, buyer=mandate.buyer, mandate=mandate, step_up=step_up,
        ledger_seq=entry.seq,
        product_url=product.url, sku=product.sku, title=product.title,
        image_url=product.image_url, quantity=quantity,
        unit_price_paise=live.price_paise, amount_paise=amount,
        currency=product.currency, llm_client=llm_client, llm_client_raw=llm_client_raw,
        plink_id=body.get("id", ""), short_url=body.get("short_url", ""),
        reference_id=reference_id,
    )

    if step_up is not None:
        step_up.status = StepUp.Status.APPROVED
        step_up.decided_at = timezone.now()
        step_up.plink_id = body.get("id", "")
        step_up.short_url = body.get("short_url", "")
        step_up.save(update_fields=["status", "decided_at", "plink_id", "short_url"])

    return CheckoutOutcome(
        Verdict.ALLOW, ReasonCode.ALLOWED,
        f"Approved. Pay {amount / 100:,.2f} INR at the link; the merchant is credited "
        f"directly and the order confirms by webhook.",
        payment_url=body["short_url"], amount_paise=amount,
        live_price_paise=live.price_paise, ledger_seq=entry.seq,
        sku=product.sku, title=product.title)
