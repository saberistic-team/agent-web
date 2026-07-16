"""Append-only audit trail for security-sensitive admin mutations."""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from app.actor_context import ActorContext
from app.repositories.postgres import get_repositories
from app.repositories.protocols import AuditEventRepository

logger = logging.getLogger(__name__)

REDACTED_VALUE = "[REDACTED]"

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "session_token",
        "csrf_token",
        "secret",
        "api_key",
        "stripe_secret_key",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "payment_intent",
        "brief",
        "message_body",
        "body",
        "raw_message",
        "email",
        "contact_value",
        "payment_credentials",
        "card_number",
        "cvv",
        "wallet_address",
        "checkout_url",
        "authorization",
        "cookie",
        "admin_session",
        "admin_password_hash",
        "admin_session_secret",
        "admin_login_limiter_secret",
        "admin_login_limiter_previous_secret",
        "resend_api_key",
        "plausible_api_key",
    }
)

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|secret|token|credential|session_id|payment_intent|api[_-]?key)",
    re.IGNORECASE,
)

ACTION_AUTH_LOGIN_SUCCESS = "auth.login.success"
ACTION_AUTH_LOGIN_FAILURE = "auth.login.failure"
ACTION_AUTH_LOGOUT = "auth.logout"
ACTION_IMPORT_BATCH = "import.batch"
ACTION_IMPORT_BATCH_ROLLBACK = "import.batch.rollback"
ACTION_ENTITY_DELETE = "entity.delete"
ACTION_COMPANY_UPDATE = "company.update"
ACTION_CONTACT_UPDATE = "contact.update"
ACTION_PIPELINE_UPDATE = "pipeline.update"
ACTION_SCORING_RULE_UPDATE = "scoring_rule.update"
ACTION_ANALYTICS_CONFIG_UPDATE = "analytics.config.update"
ACTION_EXPORT_REQUEST = "export.request"
ACTION_BRIEF_CONVERT = "brief.convert"
ACTION_CONTACT_RESTORE = "contact.restore"
ACTION_RESEARCH_RECORD_CREATE = "research_record.create"
ACTION_PIPELINE_ACTIVITY_CREATE = "pipeline_activity.create"


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in SENSITIVE_FIELD_NAMES:
        return True
    return bool(SENSITIVE_KEY_PATTERN.search(key_lower))


def redact_value(value: Any) -> Any:
    """Return a safe audit representation; never raises."""
    if value is None:
        return None
    if isinstance(value, dict):
        return redact_summary(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 500:
            return f"{value[:500]}…"
        return value
    return str(value)


def redact_summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip secrets and oversized values from before/after summaries."""
    if data is None:
        return None
    safe: dict[str, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            logger.debug("Redacted sensitive audit field: %s", key)
            safe[key] = REDACTED_VALUE
        else:
            safe[key] = redact_value(value)
    return safe


def record_event(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
    required: bool = True,
) -> dict[str, Any] | None:
    """Persist one append-only audit event.

    When ``required`` is True (default), persistence failures propagate so the
    caller's unit-of-work can roll back the related business mutation. When False,
    failures are logged and ``None`` is returned (best-effort logging only).
    """
    repo = repository or get_repositories().audit_events
    try:
        return repo.append(
            conn,
            actor=actor_context.actor,
            action=action,
            correlation_id=actor_context.correlation_id,
            entity_type=entity_type,
            entity_id=entity_id,
            summary_before=redact_summary(summary_before),
            summary_after=redact_summary(summary_after),
            metadata=redact_summary(metadata),
        )
    except Exception:
        if required:
            raise
        logger.exception("Failed to record audit event: %s", action)
        return None


def list_events(
    conn: psycopg.Connection,
    *,
    page: int = 1,
    per_page: int = 50,
    repository: AuditEventRepository | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return a paginated, newest-first audit page."""
    repo = repository or get_repositories().audit_events
    safe_page = max(page, 1)
    safe_per_page = min(max(per_page, 1), 100)
    return repo.list_page(conn, page=safe_page, per_page=safe_per_page)


def record_login_success(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    session_id: int,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_AUTH_LOGIN_SUCCESS,
        entity_type="admin_session",
        entity_id=str(session_id),
        summary_after={"session_id": session_id},
        repository=repository,
    )


def record_login_failure(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    reason: str,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {"reason": reason}
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_AUTH_LOGIN_FAILURE,
        entity_type="admin_session",
        summary_after=metadata,
        metadata=metadata,
        repository=repository,
        required=False,
    )


def record_logout(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    session_id: int | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_AUTH_LOGOUT,
        entity_type="admin_session",
        entity_id=str(session_id) if session_id is not None else None,
        summary_before={"session_id": session_id} if session_id is not None else None,
        repository=repository,
        required=session_id is not None,
    )


def record_import_batch(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    batch_id: str,
    source_type: str,
    record_count: int,
    schema_version: str | None = None,
    checksum: str | None = None,
    export_date: Any | None = None,
    summary_counts: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    summary_after: dict[str, Any] = {
        "source_type": source_type,
        "record_count": record_count,
    }
    if schema_version is not None:
        summary_after["schema_version"] = schema_version
    if checksum is not None:
        summary_after["checksum"] = checksum
    if export_date is not None:
        summary_after["export_date"] = (
            export_date.isoformat() if hasattr(export_date, "isoformat") else str(export_date)
        )
    if summary_counts is not None:
        summary_after["summary_counts"] = summary_counts
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_IMPORT_BATCH,
        entity_type="import_batch",
        entity_id=batch_id,
        summary_after=summary_after,
        repository=repository,
    )


def record_import_batch_rollback(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    batch_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_IMPORT_BATCH_ROLLBACK,
        entity_type="import_batch",
        entity_id=batch_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_entity_delete(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_type: str,
    entity_id: str,
    summary_before: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_ENTITY_DELETE,
        entity_type=entity_type,
        entity_id=entity_id,
        summary_before=summary_before,
        repository=repository,
    )


def record_company_update(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_COMPANY_UPDATE,
        entity_type="company",
        entity_id=entity_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_contact_update(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_CONTACT_UPDATE,
        entity_type="contact",
        entity_id=entity_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_pipeline_update(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_PIPELINE_UPDATE,
        entity_type="pipeline",
        entity_id=entity_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_scoring_rule_update(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    rule_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_SCORING_RULE_UPDATE,
        entity_type="scoring_rule",
        entity_id=rule_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_analytics_config_update(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    config_key: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_ANALYTICS_CONFIG_UPDATE,
        entity_type="analytics_config",
        entity_id=config_key,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def record_export_request(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    export_type: str,
    filters: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_EXPORT_REQUEST,
        entity_type="export",
        entity_id=export_type,
        summary_after={"export_type": export_type, "filters": filters or {}},
        repository=repository,
    )


def record_brief_convert(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    brief_id: str,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_BRIEF_CONVERT,
        entity_type="project_brief",
        entity_id=brief_id,
        summary_after=summary_after,
        repository=repository,
    )


def record_contact_restore(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    contact_id: str,
    summary_before: dict[str, Any] | None = None,
    summary_after: dict[str, Any] | None = None,
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_CONTACT_RESTORE,
        entity_type="contact",
        entity_id=contact_id,
        summary_before=summary_before,
        summary_after=summary_after,
        repository=repository,
    )


def research_record_audit_summary(
    *,
    research_record_id: str,
    company_id: str,
    contact_id: str | None,
    record_type: str,
    source_name: str | None = None,
    source_url: str | None = None,
    observed_value: str | None = None,
    observed_at: Any | None = None,
    confidence: float | None = None,
    review_at: Any | None = None,
    expires_at: Any | None = None,
) -> dict[str, Any]:
    """Bounded audit metadata for research evidence — no body, URLs, or values."""
    return {
        "research_record_id": research_record_id,
        "company_id": company_id,
        "contact_id": contact_id,
        "record_type": record_type,
        "has_source_name": bool(source_name and str(source_name).strip()),
        "has_source_url": bool(source_url and str(source_url).strip()),
        "has_observed_value": bool(observed_value and str(observed_value).strip()),
        "has_observed_at": observed_at is not None,
        "has_confidence": confidence is not None,
        "has_review_at": review_at is not None,
        "has_expires_at": expires_at is not None,
    }


def record_research_record_create(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    research_record_id: str,
    summary_after: dict[str, Any],
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_RESEARCH_RECORD_CREATE,
        entity_type="research_record",
        entity_id=research_record_id,
        summary_after=summary_after,
        repository=repository,
    )


def pipeline_activity_audit_summary(
    *,
    activity_id: str,
    company_id: str,
    contact_id: str | None,
    activity_type: str,
    created_at: Any,
) -> dict[str, Any]:
    """Bounded audit metadata for pipeline activity — no summary or metadata."""
    created = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at)
    )
    return {
        "activity_id": activity_id,
        "company_id": company_id,
        "contact_id": contact_id,
        "activity_type": activity_type,
        "created_at": created,
    }


def record_pipeline_activity_create(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    activity_id: str,
    summary_after: dict[str, Any],
    repository: AuditEventRepository | None = None,
) -> dict[str, Any] | None:
    return record_event(
        conn,
        actor_context=actor_context,
        action=ACTION_PIPELINE_ACTIVITY_CREATE,
        entity_type="pipeline_activity",
        entity_id=activity_id,
        summary_after=summary_after,
        repository=repository,
    )
