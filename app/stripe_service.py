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


def _stripe_object_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        raw_id = value.get("id")
        if raw_id is None:
            return None
        return str(raw_id).strip() or None
    return None


def extract_payment_details_from_session(session: dict[str, Any]) -> dict[str, Any]:
    """Read completed Checkout Session amounts and applied discount identifiers."""
    total_details = session.get("total_details") or {}
    discount_cents = total_details.get("amount_discount")
    if discount_cents is None:
        discount_cents = 0

    promotion_code_id: str | None = None
    coupon_id: str | None = None
    for discount in session.get("discounts") or []:
        if not isinstance(discount, dict):
            continue
        promotion_code_id = _stripe_object_id(discount.get("promotion_code"))
        coupon_id = _stripe_object_id(discount.get("coupon"))
        if promotion_code_id or coupon_id:
            break

    currency = session.get("currency")
    return {
        "payment_subtotal_cents": session.get("amount_subtotal"),
        "payment_discount_cents": discount_cents,
        "payment_amount_cents": session.get("amount_total"),
        "payment_currency": str(currency).lower() if currency else None,
        "stripe_promotion_code_id": promotion_code_id,
        "stripe_coupon_id": coupon_id,
    }
