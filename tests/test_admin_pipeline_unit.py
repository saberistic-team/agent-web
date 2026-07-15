"""Unit tests for pipeline admin HTML."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.admin_pipeline_pages import render_pipeline_detail_page, render_pipeline_list_page


COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_list_page_renders_rows() -> None:
    html = render_pipeline_list_page(
        companies=[
            {
                "id": COMPANY_ID,
                "name": "Northwind Labs",
                "pipeline_stage": "qualified",
                "expected_value_cents": 50_000,
                "next_action": "Send outreach",
                "next_action_due_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
                "pipeline_owner": "alex",
            }
        ],
        stage_filter=None,
        csrf_token="csrf",
        admin_username="operator",
    )
    assert "Northwind Labs" in html
    assert "Qualified" in html
    assert "Send outreach" in html


@pytest.mark.unit
def test_pipeline_detail_page_renders_history_and_forms() -> None:
    html = render_pipeline_detail_page(
        company={
            "id": COMPANY_ID,
            "name": "Northwind Labs",
            "pipeline_stage": "contacted",
            "next_action": "Follow up",
            "next_action_due_at": datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            "pipeline_owner": "alex",
            "expected_value_cents": 75_000,
        },
        history=[
            {
                "changed_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
                "from_stage": "qualified",
                "to_stage": "contacted",
                "changed_by": "operator",
            }
        ],
        activities=[
            {
                "created_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
                "activity_type": "outreach",
                "summary": "Sent intro email",
            }
        ],
        csrf_token="csrf",
        admin_username="operator",
    )
    assert "Stage history" in html
    assert "Sent intro email" in html
    assert 'name="to_stage"' in html
    assert 'name="activity_type"' in html
