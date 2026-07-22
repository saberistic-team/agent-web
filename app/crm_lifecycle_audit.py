"""Audit helpers for company/contact lifecycle mutations (#333)."""

from __future__ import annotations

from typing import Any

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.companies import company_audit_summary
from app.contacts import contact_audit_summary
from app.crm_audit import (
    COMPANY_PRESENCE_FIELDS,
    COMPANY_PRESENCE_SUMMARY_KEYS,
    CONTACT_PRESENCE_FIELDS,
    CONTACT_PRESENCE_SUMMARY_KEYS,
    changed_audit_summaries,
)


def _format_archived_at(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def company_transition_summary(company: dict[str, Any]) -> dict[str, Any]:
    """Bounded archive/restore transition metadata — no notes or funding text."""
    return {
        "archived_at": _format_archived_at(company.get("archived_at")),
        "name": company.get("name"),
    }


def contact_transition_summary(contact: dict[str, Any]) -> dict[str, Any]:
    """Bounded archive/restore transition metadata — no email or profile URL."""
    return {
        "archived_at": _format_archived_at(contact.get("archived_at")),
        "full_name": contact.get("full_name"),
    }


def record_company_update_if_changed(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_id: str,
    before_row: dict[str, Any],
    after_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Write ``company.update`` only when audit-visible fields differ (no-op skip).

    Uses :func:`changed_audit_summaries` so redacted content replacements
    (notes / website / funding text) still produce an event without storing
    the free-form values.
    """
    summary_before, summary_after = changed_audit_summaries(
        before_row,
        after_row,
        summarize=company_audit_summary,
        presence_fields=COMPANY_PRESENCE_FIELDS,
        presence_summary_keys=COMPANY_PRESENCE_SUMMARY_KEYS,
    )
    if summary_before is None or summary_after is None:
        return None
    return audit_service.record_company_update(
        conn,
        actor_context=actor_context,
        entity_id=entity_id,
        summary_before=summary_before,
        summary_after=summary_after,
    )


def record_contact_update_if_changed(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    entity_id: str,
    before_row: dict[str, Any],
    after_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Write ``contact.update`` only when audit-visible fields differ (no-op skip)."""
    summary_before, summary_after = changed_audit_summaries(
        before_row,
        after_row,
        summarize=contact_audit_summary,
        presence_fields=CONTACT_PRESENCE_FIELDS,
        presence_summary_keys=CONTACT_PRESENCE_SUMMARY_KEYS,
    )
    if summary_before is None or summary_after is None:
        return None
    return audit_service.record_contact_update(
        conn,
        actor_context=actor_context,
        entity_id=entity_id,
        summary_before=summary_before,
        summary_after=summary_after,
    )


def record_company_create(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    company: dict[str, Any],
) -> dict[str, Any] | None:
    return audit_service.record_company_create(
        conn,
        actor_context=actor_context,
        entity_id=str(company["id"]),
        summary_after=company_audit_summary(company),
    )


def record_contact_create(
    conn: psycopg.Connection,
    *,
    actor_context: ActorContext,
    contact: dict[str, Any],
) -> dict[str, Any] | None:
    return audit_service.record_contact_create(
        conn,
        actor_context=actor_context,
        entity_id=str(contact["id"]),
        summary_after=contact_audit_summary(contact),
    )
