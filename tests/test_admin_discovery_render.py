"""Unit tests for admin discovery HTML renderers."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.admin_discovery import render_discovery_run_detail_page, render_discovery_runs_page
from app.admin_discovery_pages import render_discovery_inbox_page


@pytest.mark.unit
@pytest.mark.integration
def test_render_discovery_runs_page_empty_state() -> None:
    html = render_discovery_runs_page(
        runs=[],
        page=1,
        per_page=50,
        total=0,
        admin_username="operator",
        csrf_token="csrf",
        schedule_interval_days=7,
    )
    assert "No discovery runs yet." in html
    assert "Run discovery now" in html
    assert '<a class="cta" href="/admin/discovery/inbox">Review inbox</a>' in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_discovery_inbox_page_links_back_to_runs() -> None:
    html = render_discovery_inbox_page(
        candidates=[],
        filters={},
        filter_metadata={},
        csrf_token="csrf",
        admin_username="operator",
    )
    assert '<p class="admin-breadcrumb"><a href="/admin/discovery">Discovery runs</a></p>' in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_discovery_run_detail_serializes_json_errors() -> None:
    run_id = uuid4()
    html = render_discovery_run_detail_page(
        run={
            "id": run_id,
            "trigger_type": "manual",
            "status": "failed",
            "actor": "operator",
            "started_at": "2026-07-01T00:00:00+00:00",
            "finished_at": "2026-07-01T00:01:00+00:00",
            "enabled_sources": ["ycombinator"],
            "lock_acquired": False,
            "correlation_id": "corr-1",
            "error_message": "Run failed safely",
        },
        sources=[
            {
                "source_id": "ycombinator",
                "status": "failed",
                "fetched_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "error_count": 1,
                "checkpoint_cursor": "1",
                "checkpoint_etag": 'W/"etag"',
                "checkpoint_last_modified": "Mon, 01 Jul 2026 00:00:00 GMT",
                "errors": '[{"code":"adapter_failure","message":"upstream down"}]',
            }
        ],
        admin_username="operator",
        csrf_token="csrf",
    )
    assert "upstream down" in html
    assert "Run failed safely" in html
    assert "etag=" in html
    assert f'href="/admin/discovery/inbox?run_id={run_id}"' in html
