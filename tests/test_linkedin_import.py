"""Unit tests for LinkedIn import helpers."""

from __future__ import annotations

import pytest

from app.linkedin_import import (
    LINKEDIN_IMPORT_SCHEMA_VERSION,
    compute_import_checksum,
    contact_matches_snapshot,
    contact_needs_update,
    normalize_connection_row,
    parse_export_date,
    snapshot_contact,
)


@pytest.mark.unit
def test_normalize_connection_row_maps_linkedin_headers() -> None:
    identity = normalize_connection_row(
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Company": "Analytical Engines",
            "Position": "Mathematician",
            "Connected On": "15 Jan 2024",
            "URL": "https://www.linkedin.com/in/Ada-Lovelace/",
        }
    )
    assert identity["profile_url"] == "https://linkedin.com/in/ada-lovelace"
    assert identity["full_name"] == "Ada Lovelace"
    assert identity["company_name"] == "Analytical Engines"
    assert identity["title"] == "Mathematician"


@pytest.mark.unit
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
def test_contact_needs_update_detects_title_change() -> None:
    contact = {"full_name": "Ada Lovelace", "title": "Engineer"}
    identity = {"full_name": "Ada Lovelace", "title": "Mathematician"}
    assert contact_needs_update(contact, identity) is True


@pytest.mark.unit
def test_contact_matches_snapshot_compares_tracked_fields() -> None:
    contact = {
        "full_name": "Ada",
        "title": "CTO",
        "profile_url": "https://linkedin.com/in/ada",
        "company_id": None,
        "archived_at": None,
    }
    assert contact_matches_snapshot(contact, snapshot_contact(contact)) is True
    assert contact_matches_snapshot(contact, {"full_name": "Ada", "title": "CEO"}) is False


@pytest.mark.unit
def test_parse_export_date_accepts_common_formats() -> None:
    assert parse_export_date("2026-03-15").isoformat() == "2026-03-15"
    assert parse_export_date("03/15/2026").isoformat() == "2026-03-15"
    assert parse_export_date("") is None


@pytest.mark.unit
def test_schema_version_constant() -> None:
    assert LINKEDIN_IMPORT_SCHEMA_VERSION == "linkedin_export_v1"
