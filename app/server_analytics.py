"""Authoritative server-side first-party conversion analytics (#115)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import NAMESPACE_OID, uuid5

import psycopg

from app.analytics_event_schema import (
    AnalyticsEventValidationError,
    EVENT_CHECKOUT_CANCELLED,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_NOTIFICATION_OUTCOME,
    EVENT_PAYMENT_COMPLETED,
    LinkageState,
    PathClass,
    SERVER_UNLINKED_SESSION_ID,
    build_event_payload,
    is_valid_anonymous_session_id,
)
from app.analytics_ingest import persist_analytics_event, touch_analytics_session
from app.config import Settings

logger = logging.getLogger(__name__)

NotificationKind = Literal[
    "lead_team",
    "lead_customer",
    "paid_team",
    "paid_customer",
]
NotificationOutcome = Literal["sent", "failed", "skipped"]

_FUNNEL_LEAD = 5
_FUNNEL_CHECKOUT = 6
_FUNNEL_PAYMENT = 7


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def server_idempotency_key(*parts: str | int) -> str:
    """Deterministic UUID idempotency key for server events."""
    name = ":".join(str(part) for part in parts)
    return str(uuid5(NAMESPACE_OID, name))


def resolve_analytics_session_id(
    token: str | None,
) -> tuple[str, bool]:
    """Return (session_id, session_linked). Unlinked uses server sentinel."""
    if token and is_valid_anonymous_session_id(token):
        return token, True
    return SERVER_UNLINKED_SESSION_ID, False


def _maybe_touch_session(
    conn: psycopg.Connection,
    *,
    session_id: str,
    session_linked: bool,
) -> None:
    if not session_linked:
        return
    try:
        touch_analytics_session(conn, session_id=session_id, now=_utc_now())
    except Exception:
        logger.exception("Failed to touch analytics session %s", session_id)


def record_server_event(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    event_name: str,
    brief_id: int,
    idempotency_key: str,
    analytics_session_id: str | None,
    utm: dict[str, str | None] | None,
    properties: dict[str, Any] | None = None,
    linkage_source: str,
    path_class: PathClass = PathClass.BRIEF,
    session_linked: bool | None = None,
) -> bool:
    """Persist a server-authoritative event. Returns False on duplicate. Never raises."""
    if not settings.first_party_analytics_enabled:
        return False

    session_id, linked = resolve_analytics_session_id(analytics_session_id)
    if session_linked is not None:
        linked = session_linked
        if not linked:
            session_id = SERVER_UNLINKED_SESSION_ID

    props: dict[str, Any] = {
        "brief_id": brief_id,
        "linkage_source": linkage_source,
        "environment": settings.analytics_environment,
    }
    if properties:
        props.update(properties)

    try:
        event = build_event_payload(
            event_name=event_name,
            anonymous_session_id=session_id,
            path_class=path_class,
            attribution=utm,
            properties=props,
            linkage_state=LinkageState.CRM_BRIEF_LINKED,
        )
    except AnalyticsEventValidationError:
        logger.exception(
            "Server analytics validation failed for %s brief %s",
            event_name,
            brief_id,
        )
        return False

    _maybe_touch_session(conn, session_id=session_id, session_linked=linked)

    try:
        return persist_analytics_event(
            conn,
            idempotency_key=idempotency_key,
            event=event,
            received_at=_utc_now(),
        )
    except Exception:
        logger.exception(
            "Server analytics persistence failed for %s brief %s",
            event_name,
            brief_id,
        )
        return False


def record_lead_persisted(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    brief_id: int,
    utm: dict[str, str | None] | None,
    analytics_session_id: str | None,
) -> bool:
    return record_server_event(
        settings,
        conn,
        event_name=EVENT_LEAD_PERSISTED,
        brief_id=brief_id,
        idempotency_key=server_idempotency_key("lead-persisted", brief_id),
        analytics_session_id=analytics_session_id,
        utm=utm,
        properties={"funnel_step": _FUNNEL_LEAD},
        linkage_source="server_brief_persist",
    )


def record_checkout_opened(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    brief_id: int,
    price_cents: int,
    utm: dict[str, str | None] | None,
    analytics_session_id: str | None,
) -> bool:
    return record_server_event(
        settings,
        conn,
        event_name=EVENT_CHECKOUT_OPENED,
        brief_id=brief_id,
        idempotency_key=server_idempotency_key("checkout-opened", brief_id),
        analytics_session_id=analytics_session_id,
        utm=utm,
        properties={"price_cents": price_cents, "funnel_step": _FUNNEL_CHECKOUT},
        linkage_source="server_checkout_open",
    )


def record_payment_completed(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    brief_id: int,
    price_cents: int,
    utm: dict[str, str | None] | None,
    analytics_session_id: str | None,
    stripe_event_id: str | None = None,
) -> bool:
    suffix = stripe_event_id or "default"
    return record_server_event(
        settings,
        conn,
        event_name=EVENT_PAYMENT_COMPLETED,
        brief_id=brief_id,
        idempotency_key=server_idempotency_key("payment-completed", brief_id, suffix),
        analytics_session_id=analytics_session_id,
        utm=utm,
        properties={"price_cents": price_cents, "funnel_step": _FUNNEL_PAYMENT},
        linkage_source="server_payment_complete",
        path_class=PathClass.BRIEF_SUCCESS,
    )


def record_checkout_cancelled(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    brief_id: int,
    utm: dict[str, str | None] | None,
    analytics_session_id: str | None,
    stripe_event_id: str | None = None,
) -> bool:
    suffix = stripe_event_id or "default"
    return record_server_event(
        settings,
        conn,
        event_name=EVENT_CHECKOUT_CANCELLED,
        brief_id=brief_id,
        idempotency_key=server_idempotency_key("checkout-cancelled", brief_id, suffix),
        analytics_session_id=analytics_session_id,
        utm=utm,
        properties={"funnel_step": _FUNNEL_CHECKOUT},
        linkage_source="server_checkout_cancelled",
    )


def record_notification_outcome(
    settings: Settings,
    conn: psycopg.Connection,
    *,
    brief_id: int,
    notification_kind: NotificationKind,
    notification_outcome: NotificationOutcome,
    utm: dict[str, str | None] | None,
    analytics_session_id: str | None,
) -> bool:
    return record_server_event(
        settings,
        conn,
        event_name=EVENT_NOTIFICATION_OUTCOME,
        brief_id=brief_id,
        idempotency_key=server_idempotency_key(
            "notification",
            brief_id,
            notification_kind,
            notification_outcome,
        ),
        analytics_session_id=analytics_session_id,
        utm=utm,
        properties={
            "notification_kind": notification_kind,
            "notification_outcome": notification_outcome,
        },
        linkage_source="server_notification",
    )
