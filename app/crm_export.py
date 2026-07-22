"""Safe CRM spreadsheet export — column selection and formula injection neutralization."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

import psycopg

from app.contacts import DECISION_MAKER_BUYING_ROLES
from app.repositories.protocols import ActionQueueRepository

# Columns safe for spreadsheet export — no raw messages, sessions, or credentials.
EXPORT_COLUMNS: tuple[str, ...] = (
    "company_name",
    "company_domain",
    "pipeline_stage",
    "tier",
    "target_status",
    "expected_value_usd",
    "next_action",
    "next_action_due_at",
    "contact_name",
    "contact_title",
    "contact_buying_roles",
    "contact_relationship_strength",
    "evidence_source_url",
    "evidence_confidence",
    "evidence_type",
    "unresolved_fields",
)

# Fields deliberately excluded from export (documented for tests).
EXPORT_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "email",
        "notes",
        "body",
        "raw_message",
        "message_body",
        "brief",
        "session_id",
        "analytics_session_id",
        "csrf_token",
        "admin_session",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "password",
        "password_hash",
    }
)

_FORMULA_PREFIX_CHARS = ("=", "+", "-", "@", "\t", "\r")


def neutralize_csv_cell(value: Any) -> str:
    """Neutralize spreadsheet formula injection for a single cell value."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if text[0] in _FORMULA_PREFIX_CHARS:
        return "'" + text
    return text


def _format_usd(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"{cents / 100:.2f}"


def _interim_tier(target_status: str | None, pipeline_stage: str | None) -> str:
    if target_status == "target" and pipeline_stage in ("qualified", "ready_for_outreach"):
        return "A"
    if target_status == "watching" and pipeline_stage in ("qualified", "ready_for_outreach"):
        return "B"
    return ""


def _unresolved_fields(row: dict[str, Any]) -> str:
    missing: list[str] = []
    if not row.get("next_action"):
        missing.append("next_action")
    if not row.get("next_action_due_at"):
        missing.append("next_action_due_at")
    if not row.get("has_decision_maker"):
        missing.append("decision_maker_contact")
    if not row.get("domain"):
        missing.append("domain")
    if not row.get("category"):
        missing.append("category")
    return "; ".join(missing)


def _format_dt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def build_acquisition_export_rows(
    conn: psycopg.Connection,
    repo: ActionQueueRepository,
    *,
    limit: int = 500,
) -> list[dict[str, str]]:
    """Build safe export rows for qualified pipeline prospects."""
    rows = repo.list_export_candidates(conn, limit=limit)
    export_rows: list[dict[str, str]] = []
    for row in rows:
        buying_roles = row.get("buying_roles") or []
        if isinstance(buying_roles, list):
            roles_text = ", ".join(str(r) for r in buying_roles)
        else:
            roles_text = str(buying_roles)
        confidence = row.get("evidence_confidence")
        conf_text = f"{float(confidence):.2f}" if confidence is not None else ""
        export_rows.append(
            {
                "company_name": neutralize_csv_cell(row.get("company_name")),
                "company_domain": neutralize_csv_cell(row.get("domain")),
                "pipeline_stage": neutralize_csv_cell(row.get("pipeline_stage")),
                "tier": neutralize_csv_cell(
                    _interim_tier(row.get("target_status"), row.get("pipeline_stage"))
                ),
                "target_status": neutralize_csv_cell(row.get("target_status")),
                "expected_value_usd": neutralize_csv_cell(
                    _format_usd(row.get("expected_value_cents"))
                ),
                "next_action": neutralize_csv_cell(row.get("next_action")),
                "next_action_due_at": neutralize_csv_cell(
                    _format_dt(row.get("next_action_due_at"))
                ),
                "contact_name": neutralize_csv_cell(row.get("contact_name")),
                "contact_title": neutralize_csv_cell(row.get("contact_title")),
                "contact_buying_roles": neutralize_csv_cell(roles_text),
                "contact_relationship_strength": neutralize_csv_cell(
                    row.get("relationship_strength")
                ),
                "evidence_source_url": neutralize_csv_cell(row.get("evidence_source_url")),
                "evidence_confidence": neutralize_csv_cell(conf_text),
                "evidence_type": neutralize_csv_cell(row.get("evidence_type")),
                "unresolved_fields": neutralize_csv_cell(_unresolved_fields(row)),
            }
        )
    return export_rows


def render_acquisition_export_csv(
    conn: psycopg.Connection,
    repo: ActionQueueRepository,
    *,
    limit: int = 500,
) -> str:
    """Render acquisition export as CSV text with neutralized cells."""
    rows = build_acquisition_export_rows(conn, repo, limit=limit)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


# Re-export for tests documenting decision-maker role boundary.
_EXPORT_DECISION_MAKER_ROLES = DECISION_MAKER_BUYING_ROLES
