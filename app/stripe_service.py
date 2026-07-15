"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


@dataclass(frozen=True)
class CheckoutPaymentDetails:
    """Amounts from a completed Stripe Checkout Session."""

    subtotal_cents: int
    discount_cents: int
    amount_cents: int
    currency: str
    promotion_code_id: str | None


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


def _extract_promotion_code_id(session: dict[str, Any]) -> str | None:
    discounts = session.get("discounts") or []
    for discount in discounts:
        if not isinstance(discount, dict):
            continue
        promo = discount.get("promotion_code")
        if isinstance(promo, str) and promo:
            return promo
        if isinstance(promo, dict):
            promo_id = promo.get("id")
            if promo_id:
                return str(promo_id)
    return None


def extract_payment_details_from_session(session: dict[str, Any]) -> CheckoutPaymentDetails:
    """Read subtotal, discount, total, currency, and promo id from a completed session."""
    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)
    amount_cents = session.get("amount_total")
    subtotal_cents = session.get("amount_subtotal")
    currency = str(session.get("currency") or "usd").lower()

    if amount_cents is None:
        if subtotal_cents is not None:
            amount_cents = int(subtotal_cents) - discount_cents
        else:
            amount_cents = 0
    else:
        amount_cents = int(amount_cents)

    if subtotal_cents is None:
        subtotal_cents = amount_cents + discount_cents
    else:
        subtotal_cents = int(subtotal_cents)

    return CheckoutPaymentDetails(
        subtotal_cents=subtotal_cents,
        discount_cents=discount_cents,
        amount_cents=amount_cents,
        currency=currency,
        promotion_code_id=_extract_promotion_code_id(session),
    )
