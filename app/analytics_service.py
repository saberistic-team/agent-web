"""Privacy-conscious conversion funnel analytics (first-party Postgres)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app import db
from app.analytics_event_schema import (
    ALLOWED_PROPERTY_NAMES,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_PAYMENT_COMPLETED,
    SENSITIVE_PROPERTY_NAMES,
    LinkageState,
    PathClass,
    build_event_payload,
    filter_properties,
    sanitize_attribution,
)
from app.analytics_ingest import persist_analytics_event
from app.config import Settings

logger = logging.getLogger(__name__)

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

# Opaque sentinel for server-authoritative events (no browser session).
SERVER_SESSION_ID = "00000000-0000-4000-a800-000000000001"

UTM_PROPERTY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
    }
)

_LINKAGE_SOURCE_BY_EVENT = {
    EVENT_LEAD_PERSISTED: "server_brief_persist",
    EVENT_CHECKOUT_OPENED: "server_checkout_open",
    EVENT_PAYMENT_COMPLETED: "server_payment_complete",
}


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _split_utm(props: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    properties = dict(props)
    attribution: dict[str, str] = {}
    for key in UTM_PROPERTY_KEYS:
        value = properties.pop(key, None)
        if value:
            attribution[key] = str(value)
    return properties, attribution


def _server_idempotency_key(event_name: str, brief_id: int) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"saberistic:analytics:{event_name}:{brief_id}",
        )
    )


def track_event(
    settings: Settings,
    *,
    event_name: str,
    props: dict[str, Any] | None = None,
    pathname: str = "/brief",
    path_class: PathClass | str | None = None,
) -> None:
    """Persist a server-side analytics event to Postgres. Never raises."""
    if not settings.first_party_analytics_enabled:
        return
    if not settings.database_configured:
        logger.warning("Analytics skipped: database not configured")
        return
    try:
        safe_props = sanitize_properties(props)
        safe_props.setdefault("environment", settings.analytics_environment)
        brief_id = safe_props.get("brief_id")
        if brief_id is not None:
            safe_props["linkage_source"] = _LINKAGE_SOURCE_BY_EVENT.get(
                event_name,
                "server_brief_persist",
            )

        properties, attribution = _split_utm(safe_props)
        event = build_event_payload(
            event_name=event_name,
            anonymous_session_id=SERVER_SESSION_ID,
            pathname=pathname,
            path_class=path_class,
            attribution=attribution,
            properties=properties,
            linkage_state=LinkageState.CRM_BRIEF_LINKED if brief_id is not None else None,
        )

        if brief_id is not None:
            idempotency_key = _server_idempotency_key(event_name, int(brief_id))
        else:
            idempotency_key = str(uuid.uuid4())

        received_at = _utc_now()
        with db.db_connection(settings.database_url) as conn:
            persist_analytics_event(
                conn,
                idempotency_key=idempotency_key,
                event=event,
                received_at=received_at,
            )
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
        pathname="/brief",
        path_class=PathClass.BRIEF,
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
        pathname="/brief",
        path_class=PathClass.BRIEF,
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
        pathname="/brief/success",
        path_class=PathClass.BRIEF_SUCCESS,
    )
