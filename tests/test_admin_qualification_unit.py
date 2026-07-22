"""Unit tests for admin qualification target list pages."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.admin_qualification_pages import render_target_detail_page, render_targets_list_page

pytestmark = [pytest.mark.unit, pytest.mark.integration]

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01")


@pytest.mark.unit
def test_render_targets_list_page_includes_columns_and_filters() -> None:
    html = render_targets_list_page(
        targets=[
            {
                "company_id": str(COMPANY_ID),
                "id": COMPANY_ID,
                "name": "Northwind Labs",
                "score": 9,
                "tier": "A",
                "stage": "seed",
                "vertical": "fintech",
                "strongest_signals": ["Target vertical"],
                "warm_path": "Sam Intro (introducer)",
                "has_warm_path": True,
                "next_action": "Review evidence",
                "evidence_freshness": "fresh",
                "missing_fields": [],
                "pipeline_stage": "qualified",
                "pipeline_owner": "alex",
                "stale_evidence": False,
            }
        ],
        filters={
            "tier": "A",
            "category": None,
            "stage": None,
            "pipeline_stage": None,
            "owner": None,
            "freshness": None,
            "warm_path": None,
        },
        working_lists=[{"name": "Q3 shortlist", "item_count": 2, "updated_at": datetime.now(timezone.utc)}],
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Target lists" in html
    assert "Tier A (8–10)" in html
    assert "Northwind Labs" in html
    assert "Warm path" in html
    assert "Missing fields" in html
    assert "Save working list" in html
    assert "Q3 shortlist" in html
    assert f"/admin/targets/{COMPANY_ID}" in html


@pytest.mark.unit
def test_render_target_detail_page_shows_tier_history() -> None:
    html = render_target_detail_page(
        company={"id": COMPANY_ID, "name": "Northwind Labs"},
        target={
            "tier": "A",
            "score": 9,
            "strongest_signals": ["Target vertical"],
            "warm_path": "Sam Intro",
            "evidence_freshness": "fresh",
            "missing_fields": [],
            "pipeline_stage": "qualified",
            "stale_evidence": False,
        },
        tier_history=[
            {
                "changed_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "from_tier": "B",
                "to_tier": "A",
                "score": 9,
                "changed_by": "admin",
            }
        ],
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Tier changes" in html
    assert "Northwind Labs" in html
    assert "distinct from pipeline stage" in html


@pytest.mark.unit
def test_render_targets_list_page_empty_and_error_states() -> None:
    html = render_targets_list_page(
        targets=[],
        filters={key: None for key in ("tier", "category", "stage", "pipeline_stage", "owner", "freshness", "warm_path")},
        working_lists=[],
        csrf_token="csrf",
        admin_username="admin",
        save_error="Name required",
    )
    assert "No active targets match these filters" in html
    assert "No saved working lists yet" in html
    assert "Name required" in html


@pytest.mark.unit
def test_render_target_detail_page_below_threshold_and_preview_banner() -> None:
    html = render_target_detail_page(
        company={"id": COMPANY_ID, "name": "Low Score Co"},
        target=None,
        tier_history=[],
        csrf_token="csrf",
        admin_username="admin",
        preview_banner="Preview data — not production",
    )
    assert "below active target threshold" in html
    assert "Preview data — not production" in html
    assert "No tier changes recorded yet" in html
