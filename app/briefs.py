"""Project brief validation, checkout creation, and webhook handling."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app.db import (
    CONTACT_EMAIL,
    CONTACT_PHONE,
    create_brief,
    get_brief,
    mark_paid,
    set_stripe_session,
)
from app.email_notify import send_paid_notifications

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")


class BriefCreateRequest(BaseModel):
    website: str = Field(min_length=3, max_length=2048)
    brief: str = Field(min_length=10, max_length=20_000)
    contact_method: str
    contact_value: str = Field(min_length=3, max_length=512)


class BriefCreateResponse(BaseModel):
    checkout_url: str
    brief_id: int


def _normalize_website(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if not parsed.netloc:
        raise HTTPException(status_code=422, detail="Invalid website URL")
    return value if "://" in value else f"https://{value}"


def _validate_contact(method: str, value: str) -> tuple[str, str]:
    method = method.strip().lower()
    value = value.strip()
    if method not in {CONTACT_EMAIL, CONTACT_PHONE}:
        raise HTTPException(status_code=422, detail="contact_method must be email or phone")
    if method == CONTACT_EMAIL and not _EMAIL_RE.match(value):
        raise HTTPException(status_code=422, detail="Invalid email address")
    if method == CONTACT_PHONE and not _PHONE_RE.match(value):
        raise HTTPException(status_code=422, detail="Invalid phone number")
    return method, value


def _stripe_client() -> None:
    key = config.stripe_secret_key()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    stripe.api_key = key


@router.post("/api/project-briefs", response_model=BriefCreateResponse)
def create_project_brief(payload: BriefCreateRequest) -> BriefCreateResponse:
    website = _normalize_website(payload.website)
    contact_method, contact_value = _validate_contact(
        payload.contact_method,
        payload.contact_value,
    )
    _stripe_client()

    row = create_brief(
        website=website,
        contact_method=contact_method,
        contact_value=contact_value,
        brief=payload.brief.strip(),
    )

    success_url = f"{config.app_base_url()}/request-success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{config.app_base_url()}/request-brief?cancelled=1"

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": config.BRIEF_PRICE_CENTS,
                    "product_data": {
                        "name": "Project brief request",
                        "description": "One-time project brief review fee",
                    },
                },
                "quantity": 1,
            }
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"brief_id": str(row.id)},
        client_reference_id=str(row.id),
    )

    if not session.url:
        raise HTTPException(status_code=502, detail="Stripe checkout session missing URL")

    set_stripe_session(row.id, session.id)
    return BriefCreateResponse(checkout_url=session.url, brief_id=row.id)


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request) -> dict[str, str]:
    if not config.stripe_webhook_secret():
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            config.stripe_webhook_secret(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    if event["type"] == "checkout.session.completed":
        session: dict[str, Any] = event["data"]["object"]
        brief_id_raw = session.get("metadata", {}).get("brief_id")
        if not brief_id_raw:
            return {"status": "ignored"}

        brief_id = int(brief_id_raw)
        existing = get_brief(brief_id)
        if existing is None:
            return {"status": "ignored"}

        updated = mark_paid(
            brief_id,
            stripe_session_id=session.get("id", ""),
            stripe_payment_intent_id=session.get("payment_intent"),
        )
        if updated is not None:
            send_paid_notifications(updated)

    return {"status": "ok"}
