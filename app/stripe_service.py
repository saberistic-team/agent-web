"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

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


def extract_payment_details_from_session(
    session: dict[str, Any],
) -> dict[str, int | str | None]:
    """Read completed Checkout Session payment totals (source of truth for discounts)."""
    total_details = session.get("total_details") or {}
    discount_cents = total_details.get("amount_discount")
    if discount_cents is None:
        discount_cents = 0

    stripe_discount_id: str | None = None
    for entry in session.get("discounts") or []:
        if not isinstance(entry, dict):
            continue
        promotion_code = entry.get("promotion_code")
        coupon = entry.get("coupon")
        if isinstance(promotion_code, dict):
            promo_id = promotion_code.get("id")
            if promo_id:
                stripe_discount_id = str(promo_id)
                break
        if isinstance(promotion_code, str) and promotion_code:
            stripe_discount_id = promotion_code
            break
        if isinstance(coupon, dict):
            coupon_id = coupon.get("id")
            if coupon_id:
                stripe_discount_id = str(coupon_id)
                break
        if isinstance(coupon, str) and coupon:
            stripe_discount_id = coupon
            break

    subtotal = session.get("amount_subtotal")
    total = session.get("amount_total")
    currency = session.get("currency")

    return {
        "payment_subtotal_cents": int(subtotal) if subtotal is not None else None,
        "payment_discount_cents": int(discount_cents),
        "payment_amount_cents": int(total) if total is not None else None,
        "payment_currency": str(currency).lower() if currency else None,
        "stripe_discount_id": stripe_discount_id,
    }
