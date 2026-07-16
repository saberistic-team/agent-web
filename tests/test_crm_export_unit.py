"""Unit tests for CRM spreadsheet export safety."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.crm_export import (
    EXPORT_COLUMNS,
    EXPORT_EXCLUDED_FIELDS,
    build_acquisition_export_rows,
    neutralize_csv_cell,
    render_acquisition_export_csv,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("=HYPERLINK(\"evil\")", "'=HYPERLINK(\"evil\")"),
        ("+cmd|'/c calc'", "'+cmd|'/c calc'"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tsecret", "'\tsecret"),
        ("normal text", "normal text"),
        ("", ""),
        (None, ""),
    ],
)
def test_neutralize_csv_cell(raw: str | None, expected: str) -> None:
    assert neutralize_csv_cell(raw) == expected


@pytest.mark.unit
def test_export_excludes_sensitive_fields() -> None:
    assert "email" in EXPORT_EXCLUDED_FIELDS
    assert "session_id" in EXPORT_EXCLUDED_FIELDS
    assert "analytics_session_id" in EXPORT_EXCLUDED_FIELDS
    assert "raw_message" in EXPORT_EXCLUDED_FIELDS
    assert "body" in EXPORT_EXCLUDED_FIELDS


@pytest.mark.unit
def test_export_columns_include_required_fields() -> None:
    required = {
        "company_name",
        "pipeline_stage",
        "tier",
        "evidence_source_url",
        "evidence_confidence",
        "unresolved_fields",
        "contact_name",
        "contact_buying_roles",
    }
    assert required.issubset(set(EXPORT_COLUMNS))


@pytest.mark.unit
def test_build_export_rows_neutralizes_and_omits_sensitive() -> None:
    repo = MagicMock()
    repo.list_export_candidates.return_value = [
        {
            "company_name": "=evil",
            "domain": "acme.io",
            "pipeline_stage": "qualified",
            "target_status": "target",
            "expected_value_cents": 120_000,
            "next_action": "+ping",
            "next_action_due_at": None,
            "contact_name": "Alex",
            "contact_title": "Founder",
            "buying_roles": ["founder"],
            "relationship_strength": "warm",
            "evidence_source_url": "https://example.com/signal",
            "evidence_confidence": 0.9,
            "evidence_type": "verified_fact",
            "has_decision_maker": True,
            "category": "fintech",
            # Sensitive fields that must never appear in export output:
            "email": "secret@evil.com",
            "notes": "private note",
            "body": "raw evidence body",
            "session_id": "sess-123",
        }
    ]
    rows = build_acquisition_export_rows(MagicMock(), repo, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["company_name"].startswith("'=")
    assert row["next_action"].startswith("'+")
    assert row["tier"] == "A"
    assert row["evidence_confidence"] == "0.90"
    assert "email" not in row
    assert "notes" not in row
    assert "body" not in row
    assert "session_id" not in row


@pytest.mark.unit
def test_interim_tier_b_for_watching_qualified() -> None:
    repo = MagicMock()
    repo.list_export_candidates.return_value = [
        {
            "company_name": "Watching Co",
            "domain": "watch.io",
            "pipeline_stage": "ready_for_outreach",
            "target_status": "watching",
            "expected_value_cents": 50_000,
            "next_action": "Reach out",
            "next_action_due_at": datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
            "contact_name": "Sam",
            "contact_title": "CTO",
            "buying_roles": "founder",
            "relationship_strength": "warm",
            "evidence_source_url": None,
            "evidence_confidence": None,
            "evidence_type": None,
            "has_decision_maker": True,
            "category": "fintech",
        }
    ]
    rows = build_acquisition_export_rows(MagicMock(), repo, limit=10)
    assert rows[0]["tier"] == "B"
    assert rows[0]["next_action_due_at"] == "2026-07-16 12:00 UTC"
    assert rows[0]["expected_value_usd"] == "500.00"
    assert rows[0]["unresolved_fields"] == ""


@pytest.mark.unit
def test_unresolved_fields_lists_missing_data() -> None:
    repo = MagicMock()
    repo.list_export_candidates.return_value = [
        {
            "company_name": "Sparse Co",
            "domain": None,
            "pipeline_stage": "researching",
            "target_status": "target",
            "expected_value_cents": None,
            "next_action": None,
            "next_action_due_at": None,
            "contact_name": None,
            "contact_title": None,
            "buying_roles": None,
            "relationship_strength": None,
            "evidence_source_url": None,
            "evidence_confidence": None,
            "evidence_type": None,
            "has_decision_maker": False,
            "category": None,
        }
    ]
    rows = build_acquisition_export_rows(MagicMock(), repo, limit=10)
    unresolved = rows[0]["unresolved_fields"]
    assert "next_action" in unresolved
    assert "next_action_due_at" in unresolved
    assert "decision_maker_contact" in unresolved
    assert "domain" in unresolved
    assert "category" in unresolved


@pytest.mark.unit
def test_render_export_csv_includes_header() -> None:
    repo = MagicMock()
    repo.list_export_candidates.return_value = [
        {
            "company_name": "Acme",
            "domain": None,
            "pipeline_stage": "researching",
            "target_status": "watching",
            "expected_value_cents": None,
            "next_action": None,
            "next_action_due_at": None,
            "contact_name": None,
            "contact_title": None,
            "buying_roles": None,
            "relationship_strength": None,
            "evidence_source_url": None,
            "evidence_confidence": None,
            "evidence_type": None,
            "has_decision_maker": False,
            "category": None,
        }
    ]
    csv_text = render_acquisition_export_csv(MagicMock(), repo)
    lines = csv_text.strip().splitlines()
    assert lines[0] == ",".join(EXPORT_COLUMNS)
    assert "Acme" in lines[1]
    assert "next_action" in lines[1]
