"""Privacy-conscious conversion funnel analytics via Plausible."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.analytics_event_schema import (
    ALLOWED_PROPERTY_NAMES,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    SENSITIVE_PROPERTY_NAMES,
    filter_properties,
    sanitize_attribution,
)
from app.config import Settings

logger = logging.getLogger(__name__)

PLAUSIBLE_EVENTS_URL = "https://plausible.io/api/event"

# Re-exported for backwards-compatible imports in tests and callers.
__all__ = [
    "ALLOWED_PROPERTY_NAMES",
    "EVENT_CHECKOUT_OPENED",
    "EVENT_LEAD_PERSISTED",
    "EVENT_PAYMENT_COMPLETED",
    "SENSITIVE_PROPERTY_NAMES",
    "sanitize_properties",
    "track_checkout_opened",
    "track_event",
    "track_lead_persisted",
    "track_payment_completed",
    "utm_props_from_mapping",
]


def sanitize_properties(props: dict[str, Any] | None) -> dict[str, str | int | bool]:
    """Return only allowlisted, non-sensitive properties for analytics transport."""
    if not props:
        return {}
    sanitized = filter_properties(props)
    dropped = set(props) - set(sanitized)
    for key in dropped:
        key_lower = key.lower()
        if key_lower in SENSITIVE_PROPERTY_NAMES:
            logger.warning("Blocked sensitive analytics property: %s", key)
        elif key_lower not in ALLOWED_PROPERTY_NAMES:
            logger.warning("Blocked disallowed analytics property: %s", key)
    return sanitized


def utm_props_from_mapping(utm: dict[str, str | None] | None) -> dict[str, str]:
    return sanitize_attribution(utm)


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
    discount_cents: int | None = None,
    utm: dict[str, str | None] | None = None,
) -> None:
    props: dict[str, Any] = {
        "brief_id": brief_id,
        "price_cents": price_cents,
        "funnel_step": 7,
    }
    if discount_cents:
        props["discount_cents"] = discount_cents
    props.update(utm_props_from_mapping(utm))
    track_event(
        settings,
        event_name=EVENT_PAYMENT_COMPLETED,
        props=props,
        url=f"{settings.base_url}/brief/success",
    )
