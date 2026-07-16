"""First-party browser analytics event ingestion with abuse controls."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.analytics_event_schema import (
    ANONYMOUS_SESSION_MAX_AGE_SECONDS,
    ANONYMOUS_SESSION_ROTATION_SECONDS,
    AnalyticsEventPayload,
    AnalyticsEventValidationError,
    ConsentState,
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_NOTIFICATION_OUTCOME,
    EVENT_PAYMENT_COMPLETED,
    ENGAGEMENT_EVENT_NAMES,
    EVENT_BRIEF_FORM_STARTED,
    EVENT_BRIEF_SUCCESS_VIEWED,
    EVENT_BRIEF_VIEWED,
    EVENT_CHECKOUT_CANCELLED,
    EVENT_CONTACT_INITIATED,
    SCHEMA_VERSION,
    is_valid_anonymous_session_id,
    parse_event_payload,
)
from app.config import Settings

logger = logging.getLogger(__name__)

MAX_INGEST_BODY_BYTES = 8192
ANALYTICS_SESSION_COOKIE = "saber_analytics_sid"
ANALYTICS_SESSION_HEADER = "x-analytics-session-rotate"

# Browser may only ingest engagement + client-side conversion events.
BROWSER_INGESTIBLE_EVENTS = ENGAGEMENT_EVENT_NAMES | frozenset(
    {
        EVENT_BRIEF_VIEWED,
        EVENT_BRIEF_FORM_STARTED,
        EVENT_CONTACT_INITIATED,
        EVENT_CHECKOUT_CANCELLED,
        EVENT_BRIEF_SUCCESS_VIEWED,
    }
)

SERVER_ONLY_EVENTS = frozenset(
    {
        EVENT_LEAD_PERSISTED,
        EVENT_CHECKOUT_OPENED,
        EVENT_PAYMENT_COMPLETED,
        EVENT_NOTIFICATION_OUTCOME,
    }
)

_BOT_UA_PATTERN = re.compile(
    r"(bot|crawler|spider|headless|curl/|wget/|python-requests|scrapy|httpclient)",
    re.IGNORECASE,
)

_IDEMPOTENCY_KEY_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_FALLBACK_RATE_LIMIT = 30
_fallback_lock = Lock()
_fallback_buckets: dict[str, tuple[int, datetime]] = {}


class IngestRejectReason(str, Enum):
    DISABLED = "disabled"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_JSON = "invalid_json"
    VALIDATION = "validation"
    ORIGIN = "origin"
    DNT = "dnt"
    CONSENT_DECLINED = "consent_declined"
    BOT = "bot"
    RATE_LIMIT = "rate_limit"
    SERVER_EVENT = "server_event"
    SESSION = "session"
    DATABASE = "database"


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    duplicate: bool = False
    reason: IngestRejectReason | None = None
    rotate_session: bool = False
    session_id: str | None = None


class AnalyticsEventIngestRequest(BaseModel):
    """Transport wrapper for browser event ingestion."""

    idempotency_key: str
    event_name: str
    schema_version: str
    occurred_at: datetime
    anonymous_session_id: str
    path_class: str
    referrer_class: str = "direct"
    attribution: dict[str, str] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    consent_state: str = ConsentState.IMPLICIT_ANALYTICS.value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not _IDEMPOTENCY_KEY_PATTERN.match(value):
            raise ValueError("idempotency_key must be a UUID")
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("idempotency_key must be a UUID") from exc
        return value


def site_host_from_settings(settings: Settings) -> str:
    parsed = urlparse(settings.base_url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host


def is_same_origin_request(
    *,
    origin: str | None,
    referer: str | None,
    settings: Settings,
) -> bool:
    """Require same-origin Origin or Referer when present."""
    site_host = site_host_from_settings(settings)
    if not site_host:
        return False

    def _host_matches(value: str | None) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host == site_host

    if origin:
        return _host_matches(origin)
    if referer:
        return _host_matches(referer)
    return True


def is_do_not_track(dnt_header: str | None) -> bool:
    return (dnt_header or "").strip() == "1"


def is_bot_user_agent(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return bool(_BOT_UA_PATTERN.search(user_agent))


def parse_ingest_request(raw_body: bytes) -> tuple[AnalyticsEventIngestRequest | None, str | None]:
    if len(raw_body) > MAX_INGEST_BODY_BYTES:
        return None, "body_too_large"
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(data, dict):
        return None, "invalid_json"
    try:
        return AnalyticsEventIngestRequest.model_validate(data), None
    except ValidationError:
        return None, "validation"


def to_event_payload(request: AnalyticsEventIngestRequest) -> AnalyticsEventPayload:
    return parse_event_payload(
        {
            "event_name": request.event_name,
            "schema_version": request.schema_version,
            "occurred_at": request.occurred_at.isoformat(),
            "anonymous_session_id": request.anonymous_session_id,
            "path_class": request.path_class,
            "referrer_class": request.referrer_class,
            "attribution": request.attribution,
            "properties": request.properties,
            "consent_state": request.consent_state,
        }
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_ts(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _rate_limit_key(*, scope: str, identity: str) -> str:
    return f"analytics:{scope}:{identity}"


def _try_admit_fallback(key: str, *, limit: int, window_seconds: int, now: datetime) -> bool:
    with _fallback_lock:
        count, window_start = _fallback_buckets.get(key, (0, now))
        if (now - window_start).total_seconds() >= window_seconds:
            count = 0
            window_start = now
        count += 1
        _fallback_buckets[key] = (count, window_start)
        return count <= limit


def try_admit_analytics_event(
    conn: psycopg.Connection | None,
    *,
    session_key: str,
    source_key: str,
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> bool:
    keys = (
        _rate_limit_key(scope="session", identity=session_key),
        _rate_limit_key(scope="source", identity=source_key),
    )
    if conn is None:
        return all(
            _try_admit_fallback(key, limit=rate_limit, window_seconds=window_seconds, now=now)
            for key in keys
        )

    ordered_keys = tuple(sorted(keys))
    with conn.cursor() as cur:
        for limiter_key in ordered_keys:
            cur.execute(
                """
                INSERT INTO analytics_event_rate_limits (
                    limiter_key, event_count, window_started_at, locked_until, updated_at
                )
                VALUES (%s, 0, %s, NULL, %s)
                ON CONFLICT (limiter_key) DO NOTHING
                """,
                (limiter_key, now, now),
            )

        cur.execute(
            """
            SELECT limiter_key, event_count, window_started_at, locked_until
            FROM analytics_event_rate_limits
            WHERE limiter_key = ANY(%s)
            ORDER BY limiter_key
            FOR UPDATE
            """,
            (list(ordered_keys),),
        )
        rows = {str(row["limiter_key"]): row for row in cur.fetchall()}

        for limiter_key in ordered_keys:
            if limiter_key not in rows:
                rows[limiter_key] = {
                    "limiter_key": limiter_key,
                    "event_count": 0,
                    "window_started_at": now,
                    "locked_until": None,
                }

        for limiter_key in ordered_keys:
            row = rows[limiter_key]
            locked_until = _normalize_ts(row["locked_until"])
            if locked_until is not None and locked_until > now:
                conn.commit()
                return False

        updates: dict[str, tuple[int, datetime, datetime | None]] = {}
        for limiter_key in ordered_keys:
            row = rows[limiter_key]
            window_started_at = _normalize_ts(row["window_started_at"]) or now
            if (now - window_started_at).total_seconds() >= window_seconds:
                event_count = 1
                window_started_at = now
                locked_until = None
            else:
                event_count = int(row["event_count"]) + 1
                locked_until = _normalize_ts(row["locked_until"])
            if event_count >= rate_limit:
                locked_until = now + timedelta(seconds=lockout_seconds)
            updates[limiter_key] = (event_count, window_started_at, locked_until)

        for limiter_key, (event_count, window_started_at, locked_until) in updates.items():
            cur.execute(
                """
                UPDATE analytics_event_rate_limits
                SET event_count = %s,
                    window_started_at = %s,
                    locked_until = %s,
                    updated_at = %s
                WHERE limiter_key = %s
                """,
                (event_count, window_started_at, locked_until, now, limiter_key),
            )

        conn.commit()
    return True


def touch_analytics_session(
    conn: psycopg.Connection,
    *,
    session_id: str,
    now: datetime,
) -> tuple[bool, bool]:
    """Return (valid, rotate_session). Invalid when expired beyond max age."""
    if not is_valid_anonymous_session_id(session_id):
        return False, True

    expires_at = now + timedelta(seconds=ANONYMOUS_SESSION_MAX_AGE_SECONDS)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at, last_seen_at, expires_at
            FROM analytics_sessions
            WHERE session_id = %s::uuid
            FOR UPDATE
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO analytics_sessions (session_id, created_at, last_seen_at, expires_at)
                VALUES (%s::uuid, %s, %s, %s)
                """,
                (session_id, now, now, expires_at),
            )
            conn.commit()
            return True, False

        created_at = _normalize_ts(row["created_at"]) or now
        last_seen_at = _normalize_ts(row["last_seen_at"]) or now
        stored_expires_at = _normalize_ts(row["expires_at"]) or expires_at

        if stored_expires_at <= now:
            conn.commit()
            return False, True

        max_age_deadline = created_at + timedelta(seconds=ANONYMOUS_SESSION_MAX_AGE_SECONDS)
        if max_age_deadline <= now:
            conn.commit()
            return False, True

        rotate = (
            now - last_seen_at
        ).total_seconds() >= ANONYMOUS_SESSION_ROTATION_SECONDS
        new_expires_at = min(
            now + timedelta(seconds=ANONYMOUS_SESSION_MAX_AGE_SECONDS),
            max_age_deadline,
        )
        cur.execute(
            """
            UPDATE analytics_sessions
            SET last_seen_at = %s,
                expires_at = %s
            WHERE session_id = %s::uuid
            """,
            (now, new_expires_at, session_id),
        )
        conn.commit()
        return True, rotate


def persist_analytics_event(
    conn: psycopg.Connection,
    *,
    idempotency_key: str,
    event: AnalyticsEventPayload,
    received_at: datetime,
) -> bool:
    """Insert event; return False when idempotency_key already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_events (
                idempotency_key,
                event_name,
                schema_version,
                occurred_at,
                received_at,
                anonymous_session_id,
                path_class,
                referrer_class,
                attribution,
                properties,
                consent_state,
                linkage_state
            )
            VALUES (
                %s, %s, %s, %s, %s, %s::uuid, %s, %s, %s::jsonb, %s::jsonb, %s, %s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                idempotency_key,
                event.event_name,
                event.schema_version,
                event.occurred_at,
                received_at,
                event.anonymous_session_id,
                event.path_class.value,
                event.referrer_class.value,
                json.dumps(event.attribution),
                json.dumps(event.properties),
                event.consent_state.value,
                event.linkage_state.value,
            ),
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def ingest_browser_event(
    settings: Settings,
    *,
    raw_body: bytes,
    origin: str | None,
    referer: str | None,
    dnt_header: str | None,
    user_agent: str | None,
    source_key: str,
    conn: psycopg.Connection | None,
) -> IngestResult:
    if not settings.first_party_analytics_enabled:
        return IngestResult(accepted=False, reason=IngestRejectReason.DISABLED)

    if len(raw_body) > MAX_INGEST_BODY_BYTES:
        logger.info("analytics ingest rejected: %s", IngestRejectReason.BODY_TOO_LARGE.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.BODY_TOO_LARGE)

    request, parse_error = parse_ingest_request(raw_body)
    if request is None:
        logger.info("analytics ingest rejected: %s", parse_error or IngestRejectReason.VALIDATION.value)
        return IngestResult(
            accepted=False,
            reason=IngestRejectReason.BODY_TOO_LARGE
            if parse_error == "body_too_large"
            else IngestRejectReason.INVALID_JSON
            if parse_error == "invalid_json"
            else IngestRejectReason.VALIDATION,
        )

    if request.schema_version != SCHEMA_VERSION:
        logger.info("analytics ingest rejected: schema_version")
        return IngestResult(accepted=False, reason=IngestRejectReason.VALIDATION)

    if request.event_name in SERVER_ONLY_EVENTS:
        logger.info("analytics ingest rejected: %s", IngestRejectReason.SERVER_EVENT.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.SERVER_EVENT)

    if request.event_name not in BROWSER_INGESTIBLE_EVENTS:
        logger.info("analytics ingest rejected: event_name")
        return IngestResult(accepted=False, reason=IngestRejectReason.VALIDATION)

    if request.consent_state == ConsentState.DECLINED.value or is_do_not_track(dnt_header):
        logger.info("analytics ingest rejected: %s", IngestRejectReason.CONSENT_DECLINED.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.CONSENT_DECLINED)

    if not is_same_origin_request(origin=origin, referer=referer, settings=settings):
        logger.info("analytics ingest rejected: %s", IngestRejectReason.ORIGIN.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.ORIGIN)

    if is_bot_user_agent(user_agent):
        logger.info("analytics ingest rejected: %s", IngestRejectReason.BOT.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.BOT)

    try:
        event = to_event_payload(request)
    except AnalyticsEventValidationError:
        logger.info("analytics ingest rejected: %s", IngestRejectReason.VALIDATION.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.VALIDATION)

    now = _utc_now()
    if conn is not None:
        session_valid, rotate_session = touch_analytics_session(
            conn,
            session_id=request.anonymous_session_id,
            now=now,
        )
    else:
        session_valid = is_valid_anonymous_session_id(request.anonymous_session_id)
        rotate_session = False

    if not session_valid:
        logger.info("analytics ingest rejected: %s", IngestRejectReason.SESSION.value)
        return IngestResult(
            accepted=False,
            reason=IngestRejectReason.SESSION,
            rotate_session=True,
        )

    if not try_admit_analytics_event(
        conn,
        session_key=request.anonymous_session_id,
        source_key=source_key,
        now=now,
        rate_limit=settings.analytics_ingest_rate_limit,
        window_seconds=settings.analytics_ingest_rate_window_seconds,
        lockout_seconds=settings.analytics_ingest_lockout_seconds,
    ):
        logger.info("analytics ingest rejected: %s", IngestRejectReason.RATE_LIMIT.value)
        return IngestResult(accepted=False, reason=IngestRejectReason.RATE_LIMIT)

    if conn is None:
        logger.warning("analytics ingest skipped persistence: database unavailable")
        return IngestResult(
            accepted=True,
            session_id=request.anonymous_session_id,
            rotate_session=rotate_session,
        )

    try:
        inserted = persist_analytics_event(
            conn,
            idempotency_key=request.idempotency_key,
            event=event,
            received_at=now,
        )
    except Exception:
        logger.exception("analytics ingest persistence failed")
        return IngestResult(accepted=False, reason=IngestRejectReason.DATABASE)

    return IngestResult(
        accepted=True,
        duplicate=not inserted,
        session_id=request.anonymous_session_id,
        rotate_session=rotate_session,
    )
