"""Property sanitization helpers for first-party analytics transport."""

from __future__ import annotations

import logging
from typing import Any

from app.analytics_event_schema import (
    ALLOWED_PROPERTY_NAMES,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    SENSITIVE_PROPERTY_NAMES,
    filter_properties,
    sanitize_attribution,
)

logger = logging.getLogger(__name__)

# Re-exported for backwards-compatible imports in tests and callers.
__all__ = [
    "ALLOWED_PROPERTY_NAMES",
    "EVENT_CHECKOUT_OPENED",
    "EVENT_LEAD_PERSISTED",
    "EVENT_PAYMENT_COMPLETED",
    "SENSITIVE_PROPERTY_NAMES",
    "sanitize_properties",
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
