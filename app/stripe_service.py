"""Stripe Checkout and webhook helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import stripe


@dataclass(frozen=True)
class BriefCheckoutPayment:
    """Amounts and discount identifiers from a completed Checkout Session."""

    subtotal_cents: int
    discount_cents: int
    total_cents: int
    currency: str
    stripe_coupon_id: str | None
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


def extract_payment_from_session(
    session: dict[str, Any],
    *,
    list_price_cents: int,
) -> BriefCheckoutPayment:
    """Read Stripe-completed session totals; fall back to list price when absent."""
    total_raw = session.get("amount_total")
    if total_raw is None:
        return BriefCheckoutPayment(
            subtotal_cents=list_price_cents,
            discount_cents=0,
            total_cents=list_price_cents,
            currency=str(session.get("currency") or "usd"),
            stripe_coupon_id=None,
            stripe_promotion_code_id=None,
        )

    total_cents = int(total_raw)
    subtotal_cents = int(session.get("amount_subtotal") or total_cents)
    total_details = session.get("total_details") or {}
    discount_cents = int(total_details.get("amount_discount") or 0)
    currency = str(session.get("currency") or "usd")

    stripe_coupon_id: str | None = None
    stripe_promotion_code_id: str | None = None
    breakdown = total_details.get("breakdown") or {}
    for item in breakdown.get("discounts") or []:
        discount_obj = item.get("discount") or {}
        stripe_coupon_id = _stripe_object_id(discount_obj.get("coupon"))
        stripe_promotion_code_id = _stripe_object_id(discount_obj.get("promotion_code"))
        if stripe_coupon_id or stripe_promotion_code_id:
            break

    return BriefCheckoutPayment(
        subtotal_cents=subtotal_cents,
        discount_cents=discount_cents,
        total_cents=total_cents,
        currency=currency,
        stripe_coupon_id=stripe_coupon_id,
        stripe_promotion_code_id=stripe_promotion_code_id,
    )
