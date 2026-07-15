"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


@dataclass(frozen=True)
class CheckoutPaymentDetails:
    """Amounts from a completed Stripe Checkout Session (source of truth)."""

    payment_subtotal_cents: int | None
    payment_discount_cents: int | None
    payment_amount_cents: int
    payment_currency: str
    stripe_promotion_code_id: str | None


def create_checkout_session(
    *,
    secret_key: str,
    brief_id: int,
    website: str,
    base_url: str,
    price_cents: int,
) -> stripe.checkout.Session:
    stripe.api_key = secret_key
    return stripe.checkout.Session.create(
        mode="payment",
        allow_promotion_codes=True,
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": price_cents,
                    "product_data": {
                        "name": "Architecture Diagnostic",
                        "description": f"Architecture Diagnostic for {website}",
                    },
                },
                "quantity": 1,
            }
        ],
        metadata={"brief_id": str(brief_id)},
        success_url=f"{base_url}/brief/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/brief?cancelled=1",
    )


def construct_webhook_event(
    *,
    payload: bytes,
    signature: str,
    webhook_secret: str,
) -> stripe.Event:
    return stripe.Webhook.construct_event(payload, signature, webhook_secret)


def extract_brief_id_from_session(session: dict[str, Any]) -> int | None:
    metadata = session.get("metadata") or {}
    raw_id = metadata.get("brief_id")
    if raw_id is None:
        return None
    return int(raw_id)


def extract_payment_details_from_session(session: dict[str, Any]) -> CheckoutPaymentDetails:
    """Read subtotal, discount, total, currency, and Stripe promotion/coupon id."""
    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)

    amount_total = session.get("amount_total")
    if amount_total is None:
        amount_total = 0

    subtotal_raw = session.get("amount_subtotal")
    if subtotal_raw is None and discount_cents:
        subtotal_raw = int(amount_total) + discount_cents
    subtotal_cents = int(subtotal_raw) if subtotal_raw is not None else None

    currency = str(session.get("currency") or "usd")

    promotion_code_id: str | None = None
    for discount in session.get("discounts") or []:
        if not isinstance(discount, dict):
            continue
        promotion_code_id = discount.get("promotion_code") or discount.get("coupon")
        if promotion_code_id:
            break

    return CheckoutPaymentDetails(
        payment_subtotal_cents=subtotal_cents,
        payment_discount_cents=discount_cents if discount_cents else None,
        payment_amount_cents=int(amount_total),
        payment_currency=currency,
        stripe_promotion_code_id=promotion_code_id,
    )
