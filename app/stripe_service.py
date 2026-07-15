"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


@dataclass(frozen=True)
class BriefCheckoutPayment:
    """Payment breakdown from a completed Stripe Checkout Session."""

    amount_subtotal_cents: int | None
    amount_discount_cents: int | None
    amount_total_cents: int | None
    currency: str | None
    stripe_promotion_code_id: str | None
    stripe_coupon_id: str | None


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


def _stripe_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        nested = value.get("id")
        return str(nested) if nested is not None else None
    return str(value)


def extract_payment_from_session(session: dict[str, Any]) -> BriefCheckoutPayment:
    """Read completed-session totals and Stripe discount identifiers."""
    subtotal = session.get("amount_subtotal")
    total = session.get("amount_total")
    currency = session.get("currency")
    total_details = session.get("total_details") or {}
    discount = total_details.get("amount_discount")
    if discount is None and subtotal is not None and total is not None:
        discount = max(0, int(subtotal) - int(total))

    promo_id: str | None = None
    coupon_id: str | None = None
    for entry in session.get("discounts") or []:
        if isinstance(entry, dict):
            promo_id = promo_id or _stripe_id(entry.get("promotion_code"))
            coupon_id = coupon_id or _stripe_id(entry.get("coupon"))

    breakdown = total_details.get("breakdown") or {}
    for entry in breakdown.get("discounts") or []:
        discount_obj = entry.get("discount")
        if isinstance(discount_obj, dict):
            promo_id = promo_id or _stripe_id(discount_obj.get("promotion_code"))
            coupon_id = coupon_id or _stripe_id(discount_obj.get("coupon"))

    return BriefCheckoutPayment(
        amount_subtotal_cents=int(subtotal) if subtotal is not None else None,
        amount_discount_cents=int(discount) if discount is not None else None,
        amount_total_cents=int(total) if total is not None else None,
        currency=str(currency).lower() if currency else None,
        stripe_promotion_code_id=promo_id,
        stripe_coupon_id=coupon_id,
    )
