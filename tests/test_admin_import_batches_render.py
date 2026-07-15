"""Unit + integration coverage for import batch HTML helpers."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from app.admin_import_batches import (
    _esc,
    _render_batch_row,
    _summary_badges,
    render_import_batch_detail_page,
    render_import_batches_page,
)

BATCH_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
@pytest.mark.integration
def test_esc_and_summary_badges_handle_empty_values() -> None:
    assert _esc(None) == ""
    assert _esc("a&b") == "a&amp;b"
    assert _summary_badges(None) == "—"
    assert _summary_badges({}) == "—"
    assert _summary_badges({"inserted": 0, "updated": 2}) == "updated 2"


@pytest.mark.unit
@pytest.mark.integration
def test_render_import_batches_page_empty_and_populated() -> None:
    empty = render_import_batches_page(
        batches=[],
        page=1,
        per_page=50,
        total=0,
        admin_username="operator",
        csrf_token="csrf-token",
    )
    assert "No import batches yet." in empty
    assert "Page 1 · 0 total batches" in empty

    populated = render_import_batches_page(
        batches=[
            {
                "id": BATCH_ID,
                "source_type": "linkedin",
                "export_date": date(2026, 1, 15),
                "schema_version": "linkedin_export_v1",
                "checksum": "abcdef1234567890",
                "actor": "operator",
                "status": "committed",
                "summary_counts": {
                    "inserted": 2,
                    "updated": 1,
                    "unchanged": 0,
                    "skipped": 0,
                    "conflicted": 0,
                },
                "created_at": "2026-01-16T12:00:00+00:00",
            }
        ],
        page=1,
        per_page=50,
        total=1,
        admin_username="operator",
        csrf_token="csrf-token",
        preview_banner="Preview data — not production",
    )
    assert "Import batches" in populated
    assert "linkedin_export_v1" in populated
    assert "inserted 2" in populated
    assert "Preview data — not production" in populated
    assert f"/admin/imports/batches/{BATCH_ID}" in populated


@pytest.mark.unit
@pytest.mark.integration
def test_render_import_batch_detail_includes_rollback_for_committed() -> None:
    html = render_import_batch_detail_page(
        batch={
            "id": BATCH_ID,
            "source_type": "linkedin",
            "export_date": None,
            "schema_version": "linkedin_export_v1",
            "checksum": "deadbeef",
            "actor": "operator",
            "status": "committed",
            "summary_counts": {
                "inserted": 1,
                "updated": 0,
                "unchanged": 1,
                "skipped": 1,
                "conflicted": 1,
            },
            "correlation_id": "corr-1",
        },
        rows=[
            {
                "row_index": 0,
                "outcome": "inserted",
                "source_identity": {
                    "full_name": "Ada Lovelace",
                    "profile_url": "https://linkedin.com/in/ada",
                    "company_name": "Analytical Engines",
                },
                "entity_type": "contact",
                "entity_id": CONTACT_ID,
                "detail": None,
            }
        ],
        admin_username="operator",
        csrf_token="csrf-token",
        rollback_message="Could not rollback",
    )
    assert "Rollback batch" in html
    assert f'action="/admin/imports/batches/{BATCH_ID}/rollback"' in html
    assert "Could not rollback" in html
    assert "Ada Lovelace" in html
    assert "contact bbbbbbbb…" in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_import_batch_detail_hides_rollback_when_rolled_back() -> None:
    html = render_import_batch_detail_page(
        batch={
            "id": BATCH_ID,
            "source_type": "linkedin",
            "export_date": date(2026, 2, 1),
            "schema_version": "linkedin_export_v1",
            "checksum": "cafebabe",
            "actor": "operator",
            "status": "rolled_back",
            "summary_counts": {},
            "correlation_id": "corr-2",
        },
        rows=[],
        admin_username="operator",
        csrf_token="csrf-token",
        preview_banner="Preview data — not production",
    )
    assert "Rollback batch" not in html
    assert "No row outcomes recorded." in html
    assert "Preview data — not production" in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_batch_row_parses_json_identity_and_invalid_json() -> None:
    ok = _render_batch_row(
        {
            "row_index": 3,
            "outcome": "skipped",
            "source_identity": '{"full_name": "Ada", "profile_url": "https://linkedin.com/in/ada"}',
            "detail": "Missing or invalid profile URL",
        }
    )
    assert "Ada" in ok
    assert "skipped" in ok

    bad = _render_batch_row(
        {
            "row_index": 4,
            "outcome": "conflicted",
            "source_identity": "{not-json",
            "entity_type": None,
            "entity_id": None,
            "detail": None,
        }
    )
    assert "—" in bad
    assert "conflicted" in bad
