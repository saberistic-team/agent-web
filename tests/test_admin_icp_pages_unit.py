"""Unit tests for ICP admin page renderers."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.admin_icp_pages import (
    render_icp_rules_page,
    render_icp_score_detail_page,
    render_icp_scores_list_page,
)
from app.icp_scoring import default_icp_rules

pytestmark = [pytest.mark.unit, pytest.mark.integration]


@pytest.mark.unit
def test_render_icp_scores_list_page_includes_rows_and_preview_banner() -> None:
    html = render_icp_scores_list_page(
        rows=[
            {
                "company_id": str(uuid4()),
                "company_name": "Acme",
                "total_score": 7.5,
                "version_number": 1,
                "is_override": True,
                "calculated_at": datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            }
        ],
        active_version={"version_number": 1, "label": "Default"},
        csrf_token="csrf-token",
        admin_username="operator",
        preview_banner="Preview data — not production",
    )
    assert "ICP scores" in html
    assert "Acme" in html
    assert "Preview data" in html
    assert "icp-score-cell--override" in html


@pytest.mark.unit
def test_render_icp_rules_page_includes_error_and_rule_fields() -> None:
    html = render_icp_rules_page(
        rules=[rule.model_dump() for rule in default_icp_rules()],
        active_version={"version_number": 1, "label": "Default"},
        csrf_token="csrf-token",
        admin_username="operator",
        error_message="Invalid weight",
    )
    assert "vertical_fit" in html
    assert "Invalid weight" in html
    assert "Publish new rule version" in html


@pytest.mark.unit
def test_render_icp_score_detail_page_includes_breakdown_and_evidence() -> None:
    html = render_icp_score_detail_page(
        company={"id": str(uuid4()), "name": "Acme"},
        snapshot={
            "total_score": 8.0,
            "computed_score": 6.0,
            "version_number": 1,
            "calculated_at": datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            "is_override": True,
            "override_by": "operator",
            "override_reason": "Partner intro confirmed",
            "missing_inputs": ["company.stage"],
            "breakdown": [
                {
                    "rule_id": "vertical_fit",
                    "label": "Target vertical",
                    "points_awarded": 1.0,
                    "weight": 1.0,
                    "status": "scored",
                    "missing_inputs": [],
                    "evidence": [{"field": "category", "value": "fintech"}],
                }
            ],
        },
        active_version={"version_number": 1},
        csrf_token="csrf-token",
        admin_username="operator",
        error_message="Override reason is required.",
    )
    assert "Acme" in html
    assert "vertical_fit" in html
    assert "icp-score-display--override" in html
    assert "Partner intro confirmed" in html
    assert "company.stage" in html
    assert "category=fintech" in html
    assert "Override reason is required." in html


@pytest.mark.unit
def test_render_icp_score_detail_page_without_snapshot_shows_empty_state() -> None:
    html = render_icp_score_detail_page(
        company={"id": str(uuid4()), "name": "Sparse"},
        snapshot=None,
        active_version=None,
        csrf_token="csrf-token",
        admin_username="operator",
    )
    assert "No score calculated yet." in html
    assert "Manual override" in html
