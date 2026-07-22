"""Unit tests for LinkedIn import helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.linkedin_import import (
    LINKEDIN_IMPORT_SCHEMA_VERSION,
    _json_safe,
    compute_import_checksum,
    contact_matches_snapshot,
    contact_needs_update,
    empty_summary_counts,
    increment_summary,
    normalize_connection_row,
    parse_export_date,
    snapshot_contact,
)


@pytest.mark.unit
@pytest.mark.integration
def test_normalize_connection_row_maps_linkedin_headers() -> None:
    identity = normalize_connection_row(
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Company": "Analytical Engines",
            "Position": "Mathematician",
            "Connected On": "15 Jan 2024",
            "URL": "https://www.linkedin.com/in/Ada-Lovelace/",
            "Email Address": "ada@analytical.example",
        }
    )
    assert identity["profile_url"] == "https://linkedin.com/in/ada-lovelace"
    assert identity["full_name"] == "Ada Lovelace"
    assert identity["company_name"] == "Analytical Engines"
    assert identity["title"] == "Mathematician"
    assert identity["email"] == "ada@analytical.example"


@pytest.mark.unit
@pytest.mark.integration
def test_normalize_connection_row_drops_invalid_profile_url() -> None:
    identity = normalize_connection_row(
        {
            "full_name": "Ada",
            "profile_url": "https://",
        }
    )
    assert identity["profile_url"] is None
    assert identity["full_name"] == "Ada"


@pytest.mark.unit
@pytest.mark.integration
def test_compute_import_checksum_is_stable_and_order_independent() -> None:
    rows_a = [
        {"profile_url": "https://linkedin.com/in/ada", "full_name": "Ada"},
        {"profile_url": "https://linkedin.com/in/byron", "full_name": "Byron"},
    ]
    rows_b = list(reversed(rows_a))
    assert compute_import_checksum(rows_a) == compute_import_checksum(rows_b)


@pytest.mark.unit
def test_compute_import_checksum_changes_when_identity_changes() -> None:
    base = [{"profile_url": "https://linkedin.com/in/ada", "full_name": "Ada"}]
    changed = [{"profile_url": "https://linkedin.com/in/ada", "full_name": "Ada L"}]
    assert compute_import_checksum(base) != compute_import_checksum(changed)


@pytest.mark.unit
@pytest.mark.integration
def test_contact_needs_update_detects_name_and_title_changes() -> None:
    contact = {"full_name": "Ada Lovelace", "title": "Engineer"}
    assert contact_needs_update(contact, {"full_name": "Ada L", "title": "Engineer"}) is True
    assert contact_needs_update(contact, {"full_name": "Ada Lovelace", "title": "Mathematician"}) is True
    assert contact_needs_update(contact, {"full_name": "Ada Lovelace", "title": "Engineer"}) is False
    assert contact_needs_update(contact, {}) is False


@pytest.mark.unit
@pytest.mark.integration
def test_contact_matches_snapshot_compares_tracked_fields() -> None:
    contact = {
        "full_name": "Ada",
        "title": "CTO",
        "profile_url": "https://linkedin.com/in/ada",
        "email": None,
        "company_id": None,
        "archived_at": None,
        "field_sources": {},
    }
    assert contact_matches_snapshot(contact, snapshot_contact(contact)) is True
    assert contact_matches_snapshot(contact, None) is False
    assert contact_matches_snapshot(contact, {"full_name": "Ada", "title": "CEO"}) is False


@pytest.mark.unit
def test_snapshot_contact_includes_field_sources() -> None:
    contact = {
        "full_name": "Ada",
        "title": "CTO",
        "profile_url": "https://linkedin.com/in/ada",
        "email": "ada@example.com",
        "company_id": None,
        "archived_at": None,
        "field_sources": {"notes": {"source": "manual"}},
    }
    snap = snapshot_contact(contact)
    assert snap["field_sources"] == {"notes": {"source": "manual"}}
    assert snap["email"] == "ada@example.com"


@pytest.mark.unit
@pytest.mark.integration
def test_parse_export_date_accepts_common_formats() -> None:
    assert parse_export_date("2026-03-15").isoformat() == "2026-03-15"
    assert parse_export_date("03/15/2026").isoformat() == "2026-03-15"
    assert parse_export_date("15 Jan 2024").isoformat() == "2024-01-15"
    assert parse_export_date(date(2026, 7, 1)) == date(2026, 7, 1)
    assert parse_export_date("") is None
    assert parse_export_date(None) is None
    assert parse_export_date("   ") is None
    assert parse_export_date("not-a-date") is None


@pytest.mark.unit
@pytest.mark.integration
def test_summary_helpers_and_json_safe() -> None:
    summary = empty_summary_counts()
    increment_summary(summary, "inserted")
    increment_summary(summary, "insert")
    increment_summary(summary, "update")
    increment_summary(summary, "conflict")
    increment_summary(summary, "not_a_real_outcome")
    assert summary["inserted"] == 2
    assert summary["updated"] == 1
    assert summary["conflicted"] == 1
    assert summary["skipped"] == 0
    assert _json_safe(None) is None
    assert _json_safe(datetime(2026, 1, 2, tzinfo=timezone.utc)) == "2026-01-02T00:00:00+00:00"
    assert _json_safe(date(2026, 1, 2)) == "2026-01-02"
    assert _json_safe({"a": 1}) == {"a": 1}
    assert _json_safe(42) == "42"


@pytest.mark.unit
def test_schema_version_constant() -> None:
    assert LINKEDIN_IMPORT_SCHEMA_VERSION == "linkedin_export_v1"


@pytest.mark.unit
def test_normalize_connection_row_drops_invalid_email() -> None:
    identity = normalize_connection_row({"Email Address": "not-an-email", "full_name": "Ada"})
    assert identity["email"] is None
    assert identity["full_name"] == "Ada"
