"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from typing import Any, TypedDict

import stripe


class CheckoutPaymentDetails(TypedDict):
    subtotal_cents: int
    discount_cents: int
    amount_cents: int
    currency: str
    promotion_code_id: str | None
    coupon_id: str | None


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
        return value
    if isinstance(value, dict):
        raw = value.get("id")
        return str(raw) if raw else None
    return None


def extract_payment_details_from_session(session: dict[str, Any]) -> CheckoutPaymentDetails:
    """Read subtotal, discount, final amount, and promo identifiers from a completed session."""
    subtotal_cents = int(session.get("amount_subtotal") or 0)
    amount_cents = int(session.get("amount_total") or 0)
    currency = str(session.get("currency") or "usd").lower()

    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)

    promotion_code_id: str | None = None
    coupon_id: str | None = None
    breakdown = total_details.get("breakdown") or {}
    for entry in breakdown.get("discounts") or []:
        discount_obj = entry.get("discount") or {}
        promotion_code_id = _stripe_object_id(discount_obj.get("promotion_code"))
        coupon_id = _stripe_object_id(discount_obj.get("coupon"))
        if promotion_code_id or coupon_id:
            break

    if not promotion_code_id and not coupon_id:
        for entry in session.get("discounts") or []:
            if not isinstance(entry, dict):
                continue
            promotion_code_id = _stripe_object_id(entry.get("promotion_code"))
            coupon_id = _stripe_object_id(entry.get("coupon"))
            if promotion_code_id or coupon_id:
                break

    return {
        "subtotal_cents": subtotal_cents,
        "discount_cents": discount_cents,
        "amount_cents": amount_cents,
        "currency": currency,
        "promotion_code_id": promotion_code_id,
        "coupon_id": coupon_id,
    }
