"""Privacy-conscious conversion funnel analytics via Plausible."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

PLAUSIBLE_EVENTS_URL = "https://plausible.io/api/event"

# Properties that must never be sent to analytics.
SENSITIVE_PROPERTY_NAMES = frozenset(
    {
        "email",
        "phone",
        "website",
        "brief",
        "contact_value",
        "contact_method",
        "wallet_address",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "checkout_url",
        "session_id",
        "payment_intent",
        "url",
        "submitted_url",
        "query_string",
    }
)

# Allowlist for custom event properties.
ALLOWED_PROPERTY_NAMES = frozenset(
    {
        "brief_id",
        "price_cents",
        "environment",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "page",
        "contact_channel",
        "funnel_step",
        "case_study_slug",
        "article_slug",
    }
)

EVENT_LEAD_PERSISTED = "Lead Persisted"
EVENT_CHECKOUT_OPENED = "Checkout Opened"
EVENT_PAYMENT_COMPLETED = "Payment Completed"


def sanitize_properties(props: dict[str, Any] | None) -> dict[str, str | int | bool]:
    """Return only allowlisted, non-sensitive properties for analytics."""
    if not props:
        return {}
    sanitized: dict[str, str | int | bool] = {}
    for key, value in props.items():
        key_lower = key.lower()
        if key_lower in SENSITIVE_PROPERTY_NAMES:
            logger.warning("Blocked sensitive analytics property: %s", key)
            continue
        if key_lower not in ALLOWED_PROPERTY_NAMES:
            logger.warning("Blocked disallowed analytics property: %s", key)
            continue
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            sanitized[key_lower] = value
        elif isinstance(value, int):
            sanitized[key_lower] = value
        elif isinstance(value, str):
            sanitized[key_lower] = value
        else:
            sanitized[key_lower] = str(value)
    return sanitized


def utm_props_from_mapping(utm: dict[str, str | None] | None) -> dict[str, str]:
    if not utm:
        return {}
    keys = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
    return {key: value for key in keys if (value := utm.get(key))}


def track_event(
    settings: Settings,
    *,
    event_name: str,
    props: dict[str, Any] | None = None,
    url: str | None = None,
) -> None:
    """Emit a server-side Plausible event. Never raises."""
    if not settings.analytics_enabled:
        return
    try:
        safe_props = sanitize_properties(props)
        safe_props.setdefault("environment", settings.analytics_environment)

        payload: dict[str, Any] = {
            "name": event_name,
            "domain": settings.plausible_domain,
            "url": url or f"{settings.base_url}/api/internal-analytics",
            "props": safe_props,
        }
        headers = {
            "User-Agent": "saberistic-agent-web/1.0",
            "Content-Type": "application/json",
        }
        if settings.plausible_api_key:
            headers["Authorization"] = f"Bearer {settings.plausible_api_key}"

        with httpx.Client(timeout=3.0) as client:
            response = client.post(PLAUSIBLE_EVENTS_URL, json=payload, headers=headers)
            response.raise_for_status()
    except Exception:
        logger.exception("Analytics event failed: %s", event_name)


def track_lead_persisted(
    settings: Settings,
    *,
    brief_id: int,
    utm: dict[str, str | None] | None = None,
) -> None:
    props: dict[str, Any] = {"brief_id": brief_id, "funnel_step": 5}
    props.update(utm_props_from_mapping(utm))
    track_event(
        settings,
        event_name=EVENT_LEAD_PERSISTED,
        props=props,
        url=f"{settings.base_url}/brief",
    )


def track_checkout_opened(
    settings: Settings,
    *,
    brief_id: int,
    price_cents: int,
    utm: dict[str, str | None] | None = None,
) -> None:
    props: dict[str, Any] = {
        "brief_id": brief_id,
        "price_cents": price_cents,
        "funnel_step": 6,
    }
    props.update(utm_props_from_mapping(utm))
    track_event(
        settings,
        event_name=EVENT_CHECKOUT_OPENED,
        props=props,
        url=f"{settings.base_url}/brief",
    )


def track_payment_completed(
    settings: Settings,
    *,
    brief_id: int,
    price_cents: int,
    utm: dict[str, str | None] | None = None,
) -> None:
    props: dict[str, Any] = {
        "brief_id": brief_id,
        "price_cents": price_cents,
        "funnel_step": 7,
    }
    props.update(utm_props_from_mapping(utm))
    track_event(
        settings,
        event_name=EVENT_PAYMENT_COMPLETED,
        props=props,
        url=f"{settings.base_url}/brief/success",
    )
