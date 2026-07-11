"""Transactional email for paid project briefs (Resend API)."""

from __future__ import annotations

import logging

import httpx

from app import config
from app.db import ProjectBrief

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def _send_email(*, to: str, subject: str, text: str) -> bool:
    api_key = config.resend_api_key()
    if not api_key:
        logger.warning("RESEND_API_KEY not set; skipping email to %s", to)
        return False

    payload = {
        "from": config.from_email(),
        "to": [to],
        "subject": subject,
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = httpx.post(RESEND_URL, json=payload, headers=headers, timeout=15.0)
    response.raise_for_status()
    return True


def notify_inbox(brief: ProjectBrief) -> bool:
    contact_label = "Email" if brief.contact_method == "email" else "Phone"
    text = (
        "New paid project brief request\n\n"
        f"Website: {brief.website}\n"
        f"{contact_label}: {brief.contact_value}\n"
        f"Brief ID: {brief.id}\n"
        f"Stripe session: {brief.stripe_session_id or 'n/a'}\n"
        f"Payment intent: {brief.stripe_payment_intent_id or 'n/a'}\n\n"
        "Brief:\n"
        f"{brief.brief}\n"
    )
    return _send_email(
        to=config.notify_email(),
        subject=f"Paid project brief #{brief.id}",
        text=text,
    )


def notify_customer(brief: ProjectBrief) -> bool:
    if brief.contact_method != "email":
        logger.info(
            "Skipping customer email for brief %s (phone-only contact)",
            brief.id,
        )
        return False

    text = (
        "We received your project brief request.\n\n"
        "Thank you for your payment. The saberistic team will review your brief "
        "and follow up using the contact details you provided.\n\n"
        f"Website: {brief.website}\n\n"
        "— saberistic"
    )
    return _send_email(
        to=brief.contact_value,
        subject="We received your project brief request",
        text=text,
    )


def send_paid_notifications(brief: ProjectBrief) -> None:
    notify_inbox(brief)
    notify_customer(brief)
