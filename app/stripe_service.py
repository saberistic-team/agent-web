"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


@dataclass(frozen=True)
class BriefCheckoutPayment:
    """Amounts from a completed Stripe Checkout Session."""

    subtotal_cents: int
    discount_cents: int
    amount_cents: int
    currency: str
    stripe_discount_id: str | None


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


def _stripe_object_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        raw_id = value.get("id")
        if raw_id is not None and str(raw_id).strip():
            return str(raw_id).strip()
    return None


def extract_stripe_discount_id(session: dict[str, Any]) -> str | None:
    """Return Stripe promotion-code or coupon id when a discount was applied."""
    discounts = session.get("discounts") or []
    if not discounts:
        return None
    entry = discounts[0]
    if not isinstance(entry, dict):
        return None
    promo_id = _stripe_object_id(entry.get("promotion_code"))
    if promo_id:
        return promo_id
    return _stripe_object_id(entry.get("coupon"))


def extract_payment_from_session(session: dict[str, Any]) -> BriefCheckoutPayment:
    """Read subtotal, discount, total, and currency from a completed session."""
    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)
    amount_cents = int(session.get("amount_total") or 0)
    subtotal_cents = int(session.get("amount_subtotal") or amount_cents)
    currency = str(session.get("currency") or "usd").lower()
    return BriefCheckoutPayment(
        subtotal_cents=subtotal_cents,
        discount_cents=discount_cents,
        amount_cents=amount_cents,
        currency=currency,
        stripe_discount_id=extract_stripe_discount_id(session),
    )
