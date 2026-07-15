"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from typing import Any, TypedDict

import stripe


class BriefPaymentDetails(TypedDict):
    payment_subtotal_cents: int | None
    payment_discount_cents: int | None
    payment_amount_cents: int | None
    payment_currency: str | None
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


def _stripe_object_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        raw_id = value.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            return raw_id
    return None


def extract_stripe_promotion_code_id(session: dict[str, Any]) -> str | None:
    """Return Stripe promotion/coupon identifier — not the customer-entered code."""
    discounts = session.get("discounts") or []
    for discount in discounts:
        if not isinstance(discount, dict):
            continue
        promo_id = _stripe_object_id(discount.get("promotion_code"))
        if promo_id:
            return promo_id
        coupon_id = _stripe_object_id(discount.get("coupon"))
        if coupon_id:
            return coupon_id
    return None


def extract_payment_details_from_session(session: dict[str, Any]) -> BriefPaymentDetails:
    """Read completed Checkout Session totals as Stripe reported them."""
    total_details = session.get("total_details") or {}
    discount_cents = total_details.get("amount_discount")
    return BriefPaymentDetails(
        payment_subtotal_cents=session.get("amount_subtotal"),
        payment_discount_cents=discount_cents if discount_cents else None,
        payment_amount_cents=session.get("amount_total"),
        payment_currency=session.get("currency"),
        stripe_promotion_code_id=extract_stripe_promotion_code_id(session),
    )
