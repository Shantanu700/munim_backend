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
from modules.twin.orders import Order, create_order
from modules.twin.payments import Payment, create_payment, reproject
from payments.razorpay import PaymentError, create_payment_link

STEP_UP_MINUTES = 30
LINK_MINUTES = 30

MAX_PAYMENT_ATTEMPTS = 3

RETRY_COOLDOWN = timedelta(minutes=2)


class RetryRefused(Exception):
    """This order may not have another payment link, and why.

    Carries a reason_code so callers can turn it into the same
    (verdict, reason_code, message) shape every other refusal on this path uses,
    rather than a bare error string an agent has to parse prose out of.
    """

    def __init__(self, reason_code: str, message: str, *, payment_url: str = ""):
        self.reason_code = reason_code
        self.message = message
        self.payment_url = payment_url
        super().__init__(message)


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


def _link_for(tenant, mandate, product, amount_paise, quantity,
              expire_minutes=LINK_MINUTES):
    """Create the Payment Link and record it. Raises PaymentError upward.

    Returns (body, reference_id, expires_at). The reference travels back
    explicitly rather than being read out of the response, because it is the
    handle a payment.failed event resolves the order by -- and a handle that
    depends on the provider echoing a field back is a handle that can go
    missing.

    A FRESH uuid4 per attempt, never something derived from the order number:
    the reference reaches the buyer in a URL, and the store's own order counter
    is not a thing to publish there.

    expires_at is computed here rather than read off the response so the Payment
    row and the link agree on one clock -- and so the reaper has an absolute
    per-attempt deadline instead of inferring one from the order's age, which
    stops being true the moment an order has two links.
    """
    reference_id = str(uuid.uuid4())
    expires_at = timezone.now() + timedelta(minutes=expire_minutes)
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
        expire_minutes=expire_minutes,
    )
    LedgerEntry.append(
        tenant=tenant, action="payment_link_created", decision=Verdict.ALLOW,
        reason_code=ReasonCode.ALLOWED,
        detail={"plink_id": body.get("id", ""), "reference_id": reference_id,
                "amount_paise": amount_paise, "short_url": body.get("short_url", ""),
                "mandate_jti": str(mandate.jti)},
    )
    return body, reference_id, expires_at


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


def _record_attempt(tenant, *, order, mandate, step_up, entry, product, quantity,
                    live, amount, llm_client, llm_client_raw,
                    body, reference_id, expires_at):
    """The Order (if it is new) plus the Payment for this attempt.

    The only place either row is inserted on the checkout path. Both inserts
    stay AFTER the Razorpay call at every call site, for the reason
    create_order's docstring already gives: a network side effect no rollback
    can undo must not sit inside a row lock.

    Note what create_order is NOT given any more -- plink_id, short_url and
    reference_id. Those are the projection now, and create_payment writes them
    in the same transaction as the attempt they describe, so there is no window
    where an order points at a link that has no Payment row.
    """
    if order is None:
        order = create_order(
            tenant=tenant, buyer=mandate.buyer, mandate=mandate, step_up=step_up,
            ledger_seq=entry.seq,
            product_url=product.url, sku=product.sku, title=product.title,
            image_url=product.image_url, quantity=quantity,
            unit_price_paise=live.price_paise, amount_paise=amount,
            currency=product.currency,
            llm_client=llm_client, llm_client_raw=llm_client_raw,
        )
    payment = create_payment(
        order,
        plink_id=body.get("id", ""), short_url=body.get("short_url", ""),
        reference_id=reference_id, amount_paise=amount,
        currency=product.currency, expires_at=expires_at, ledger_seq=entry.seq,
    )
    return order, payment


def quote_and_gate(tenant, *, mandate_token: str, sku: str | None = None,
                   url: str | None = None, quantity: int = 1,
                   quoted_price_paise: int | None = None,
                   approved_reason: str | None = None,
                   step_up: StepUp | None = None,
                   llm_client: str = "", llm_client_raw: str = "",
                   order=None) -> CheckoutOutcome:
    """Gate a purchase and, if it passes, mint a Payment Link for it.

    `order` is the retry door and the only thing it changes. Given None -- every
    caller that is not a retry -- this mints a fresh Order and its first
    Payment. Given an existing Order, it attaches a NEW Payment to that one:
    same order number, same single row in the daily cap, a second link.

    Everything above the insert is identical either way, and that is the point.
    A retry is RE-DECIDED, not replayed: the live page is re-read, the policy
    re-evaluated, the mandate re-verified. A link minted under rules that no
    longer hold is exactly the failure the live re-read exists to prevent.
    """
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
        body, reference_id, expires_at = _link_for(tenant, mandate, product, amount,
                                                   quantity)
    except PaymentError as exc:
        LedgerEntry.append(tenant=tenant, action="payment_link_failed", decision=Verdict.DENY,
                           reason_code="PAYMENT_LINK_FAILED",
                           detail={"error": str(exc), "status": exc.status})
        return CheckoutOutcome(Verdict.DENY, "PAYMENT_LINK_FAILED", str(exc),
                               amount_paise=amount, sku=product.sku, title=product.title)

    _record_attempt(
        tenant, order=order, mandate=mandate, step_up=step_up, entry=entry,
        product=product, quantity=quantity, live=live, amount=amount,
        llm_client=llm_client, llm_client_raw=llm_client_raw,
        body=body, reference_id=reference_id, expires_at=expires_at,
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


def _stopped_being_payable(payment):
    """When this attempt actually stopped being payable, for the cooldown.

    Not closed_at, which is when we WROTE DOWN that it ended. Those differ by up
    to thirty-five minutes for an expired link -- the reaper's grace period plus
    its interval -- and they differ by however long the check below took for one
    this very call just expired inline. Debouncing off the bookkeeping moment
    would make that inline path unreachable: close the lapsed link, then
    immediately refuse the retry it was closed for.

    An expiry has a real deadline, so use it. A cancellation or a decline has
    none, so the moment we recorded it is the best available.
    """
    if payment.status == Payment.Status.EXPIRED:
        return payment.expires_at
    return payment.closed_at or payment.expires_at


def check_retryable(order, mandate_token: str, *, now=None) -> None:
    """May this order have another payment link? Raises RetryRefused if not.

    First refusal wins, and the order of the checks is the order of how much
    damage getting them wrong would do.

    The mandate check is the one worth explaining. READING an order is scoped to
    the buyer -- any live mandate of theirs shows their whole history on this
    store, which is what "what did my agent spend" has to mean. MINTING A LINK
    is scoped to the mandate itself: a retry spends budget, and re-gating under
    the order's original mandate while the caller presented a different one
    would let mandate A fund spending that mandate B's caps are supposed to
    bound. So a retry has to come from the mandate that authorised the order.

    Expiry and revocation of that mandate are deliberately NOT checked here.
    quote_and_gate re-verifies the token through jws.verify and engine.evaluate,
    which is the code that owns those rules; duplicating them here would be a
    second copy to drift.
    """
    now = now or timezone.now()

    if order.status == Order.Status.PAID:
        raise RetryRefused(
            ReasonCode.ORDER_ALREADY_PAID,
            f"Order {order.order_number} is already paid. Nothing more to collect.")

    live = order.payments.filter(status=Payment.Status.PENDING).first()
    if live is not None:
        if live.expires_at > now:
            raise RetryRefused(
                ReasonCode.PAYMENT_STILL_LIVE,
                f"Order {order.order_number} already has a payment link that is still "
                f"good. Give the buyer this one rather than making another.",
                payment_url=live.short_url)
        Payment.objects.filter(pk=live.pk, status=Payment.Status.PENDING).update(
            status=Payment.Status.EXPIRED, closed_at=now,
            failure_reason_code=ReasonCode.LINK_EXPIRED)
        reproject(order)
        order.refresh_from_db()

    if order.attempts >= MAX_PAYMENT_ATTEMPTS:
        raise RetryRefused(
            ReasonCode.RETRY_LIMIT_REACHED,
            f"Order {order.order_number} has already had {order.attempts} payment "
            f"links, which is the limit. Start a new order instead.")

    latest = order.payments.order_by("-attempt").first()
    if latest is not None and now - _stopped_being_payable(latest) < RETRY_COOLDOWN:
        raise RetryRefused(
            ReasonCode.RETRY_TOO_SOON,
            "The last payment link on this order closed moments ago. Wait a couple of "
            "minutes before making another.")

    if order.mandate is None:
        raise RetryRefused(
            ReasonCode.MANDATE_NOT_FOUND,
            "The mandate this order was placed under is gone, so it cannot be retried. "
            "Place a new order instead.")

    try:
        presented_jti = jws.verify(mandate_token).get("jti")
    except jws.MandateInvalid as exc:
        raise RetryRefused(exc.reason, str(exc.detail or exc.reason)) from exc

    if str(presented_jti) != str(order.mandate.jti):
        raise RetryRefused(
            ReasonCode.MANDATE_NOT_FOUND,
            "That mandate did not place this order. A retry has to come from the same "
            "mandate that authorised it.")


def retry_payment(order, mandate_token: str, *, llm_client: str = "",
                  llm_client_raw: str = "", now=None) -> CheckoutOutcome:
    """Mint a NEW payment link for an order that already exists.

    Re-runs the FULL gate -- live re-read, engine.evaluate, ledger append -- and
    is not a replay of the first decision. Everything that decision rested on
    can have moved since it was made: the price, the stock, the store's active
    rules, the mandate's remaining budget, the agent kill switch. A link minted
    under rules that no longer hold is exactly the failure the live re-read
    exists to prevent, and a retry is the case where the gap is widest.

    So this can legitimately come back DENY or STEP_UP. Callers must handle all
    three verdicts, not just ALLOW.

    quoted_price_paise is the order's original unit price, which makes the
    engine check the retry against what the buyer actually agreed to rather than
    against whatever the page says today -- a price that moved comes back as
    PRICE_CHANGED to re-quote, not as a silently larger charge.
    """
    check_retryable(order, mandate_token, now=now)
    return quote_and_gate(
        order.tenant, mandate_token=mandate_token,
        url=order.product_url, quantity=order.quantity,
        quoted_price_paise=order.unit_price_paise,
        llm_client=llm_client or order.llm_client,
        llm_client_raw=llm_client_raw or order.llm_client_raw,
        order=order,
    )
