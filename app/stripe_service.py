"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


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


@dataclass(frozen=True)
class BriefCheckoutPayment:
    """Payment totals from a completed Stripe Checkout Session."""

    subtotal_cents: int
    discount_cents: int
    amount_cents: int
    currency: str
    promotion_code_id: str | None
    coupon_id: str | None


def _stripe_object_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        raw_id = value.get("id")
        return str(raw_id) if raw_id else None
    return None


def extract_payment_from_session(session: dict[str, Any]) -> BriefCheckoutPayment:
    """Read subtotal, discount, and final amount from a completed checkout session."""
    amount_total = session.get("amount_total")
    if amount_total is None:
        raise ValueError("checkout session missing amount_total")

    amount_subtotal = session.get("amount_subtotal")
    if amount_subtotal is None:
        amount_subtotal = amount_total

    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)
    if discount_cents == 0 and amount_subtotal > amount_total:
        discount_cents = int(amount_subtotal) - int(amount_total)

    currency = str(session.get("currency") or "usd").lower()

    promotion_code_id: str | None = None
    coupon_id: str | None = None
    for discount in session.get("discounts") or []:
        if not isinstance(discount, dict):
            continue
        if promotion_code_id is None:
            promotion_code_id = _stripe_object_id(discount.get("promotion_code"))
        if coupon_id is None:
            coupon_id = _stripe_object_id(discount.get("coupon"))

    return BriefCheckoutPayment(
        subtotal_cents=int(amount_subtotal),
        discount_cents=discount_cents,
        amount_cents=int(amount_total),
        currency=currency,
        promotion_code_id=promotion_code_id,
        coupon_id=coupon_id,
    )
