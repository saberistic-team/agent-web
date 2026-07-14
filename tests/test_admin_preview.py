"""Tests for ADMIN_PREVIEW_MODE mock dashboard data."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from app.admin_preview import (
    COMPANY_NAMES,
    build_preview_dashboard_data,
    build_preview_section_rows,
    render_preview_dashboard_main,
    render_preview_section_main,
)


@pytest.mark.unit
def test_preview_data_is_randomized_across_seeds() -> None:
    a = build_preview_dashboard_data(rng=random.Random(1))
    b = build_preview_dashboard_data(rng=random.Random(2))
    assert a.briefs_this_week != b.briefs_this_week or a.recent_briefs != b.recent_briefs


@pytest.mark.unit
def test_preview_data_stable_with_same_seed() -> None:
    a = build_preview_dashboard_data(rng=random.Random(42))
    b = build_preview_dashboard_data(rng=random.Random(42))
    assert a == b


@pytest.mark.unit
def test_preview_data_has_plausible_ranges() -> None:
    data = build_preview_dashboard_data(
        rng=random.Random(7),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert 4 <= data.briefs_this_week <= 28
    assert 1 <= data.paid_this_week <= data.briefs_this_week
    assert 6 <= data.open_prospects <= 40
    assert 1 <= data.sessions_active <= 4
    assert 4 <= len(data.recent_briefs) <= 8
    assert data.preview_banner.startswith("Preview data")
    for row in data.recent_briefs:
        assert row.company in COMPANY_NAMES
        assert "@" in row.email
        assert row.status in {"new", "paid", "follow-up", "closed"}
        assert row.amount_cents >= 20_000


@pytest.mark.unit
def test_preview_dashboard_main_html_includes_mock_table() -> None:
    data = build_preview_dashboard_data(rng=random.Random(99))
    html = render_preview_dashboard_main(data)
    assert "Preview data — not production" in html
    assert "admin-stat-row" in html
    assert "Recent submissions" in html
    assert data.recent_briefs[0].company in html


@pytest.mark.unit
def test_preview_section_rows_stable_with_seed() -> None:
    a = build_preview_section_rows("/admin/companies", rng=random.Random(11))
    b = build_preview_section_rows("/admin/companies", rng=random.Random(11))
    assert a == b
    assert 4 <= len(a) <= 8
    assert all(len(row) == 5 for row in a)


@pytest.mark.unit
def test_preview_section_main_html_includes_mock_table() -> None:
    html = render_preview_section_main(
        label="Companies",
        summary="Company records and firmographics",
        active_path="/admin/companies",
        rng=random.Random(3),
    )
    assert "Preview data — not production" in html
    assert "Companies" in html
    assert "admin-table" in html
    assert "Industry" in html
