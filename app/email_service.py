"""Outbound email via Resend HTTP API."""

from __future__ import annotations

from typing import Any

import httpx

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(
    *,
    api_key: str,
    from_email: str,
    to: str,
    subject: str,
    text: str,
) -> dict[str, Any] | None:
    if not api_key:
        return None

    response = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [to],
            "subject": subject,
            "text": text,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def notify_team_of_paid_brief(
    *,
    api_key: str,
    from_email: str,
    notify_email: str,
    brief: dict[str, Any],
) -> dict[str, Any] | None:
    contact_label = brief["contact_method"]
    body = (
        "New paid project brief request\n\n"
        f"Website: {brief['website']}\n"
        f"Contact ({contact_label}): {brief['contact_value']}\n\n"
        f"Brief:\n{brief['brief']}\n\n"
        f"Brief ID: {brief['id']}\n"
        f"Stripe session: {brief.get('stripe_session_id') or 'n/a'}\n"
        f"Stripe payment intent: {brief.get('stripe_payment_intent_id') or 'n/a'}\n"
    )
    return send_email(
        api_key=api_key,
        from_email=from_email,
        to=notify_email,
        subject=f"Paid project brief — {brief['website']}",
        text=body,
    )


def notify_customer_of_paid_brief(
    *,
    api_key: str,
    from_email: str,
    brief: dict[str, Any],
) -> dict[str, Any] | None:
    if brief["contact_method"] != "email":
        return None

    body = (
        "We received your project brief request.\n\n"
        "Thank you for your payment. Our team will review your brief and "
        "follow up using the contact details you provided.\n\n"
        f"Website: {brief['website']}\n\n"
        "— saberistic"
    )
    return send_email(
        api_key=api_key,
        from_email=from_email,
        to=brief["contact_value"],
        subject="We received your project brief request",
        text=body,
    )
