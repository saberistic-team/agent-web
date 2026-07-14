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


@pytest.mark.unit
def test_preview_pipeline_section_uses_acquisition_stages() -> None:
    html = render_preview_section_main(
        label="Pipeline",
        summary="Acquisition pipeline stages and next actions",
        active_path="/admin/pipeline",
        rng=random.Random(17),
    )
    assert "Preview data — not production" in html
    assert "Pipeline" in html
    assert "Ready for outreach" in html or "Discovery scheduled" in html


@pytest.mark.unit
def test_preview_brief_rows_randomized_and_seed_stable() -> None:
    from app.admin_preview import build_preview_brief_detail, build_preview_brief_rows

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_brief_rows(rng=random.Random(5), now=now)
    b = build_preview_brief_rows(rng=random.Random(5), now=now)
    c = build_preview_brief_rows(rng=random.Random(6), now=now)
    assert a == b
    assert 5 <= len(a) <= 9
    assert a[0]["id"] == 1 and a[0]["status"] == "paid"
    assert a[1]["id"] == 2 and a[1]["status"] == "pending_payment"
    assert a[1]["utm_source"] is None and a[1]["paid_at"] is None
    assert a[0]["website"] != c[0]["website"] or a[0]["contact_value"] != c[0]["contact_value"]
    detail = build_preview_brief_detail(1, rng=random.Random(5), now=now)
    assert detail is not None
    assert detail["website"] == a[0]["website"]
    assert detail["brief"] == a[0]["brief"]
    assert build_preview_brief_detail(999, rng=random.Random(5), now=now) is None


@pytest.mark.unit
def test_preview_audit_events_seed_stable() -> None:
    from app.admin_preview import build_preview_audit_events

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_audit_events(rng=random.Random(9), now=now)
    b = build_preview_audit_events(rng=random.Random(9), now=now)
    assert a == b
    assert 4 <= len(a) <= 8
    assert a[0]["action"]
    assert a[0]["actor"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_preview_briefs_list_and_detail_have_mock_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    listing = client.get("/admin/briefs")
    assert listing.status_code == 200
    assert "No project briefs submitted yet." not in listing.text
    assert "brief-table" in listing.text
    assert "/admin/briefs/1" in listing.text
    detail = client.get("/admin/briefs/1")
    assert detail.status_code == 200
    assert "Project brief #1" in detail.text
    assert "Paid" in detail.text
    emptyish = client.get("/admin/briefs/2")
    assert emptyish.status_code == 200
    assert "Project brief #2" in emptyish.text
    assert "Pending" in emptyish.text
    missing = client.get("/admin/briefs/999")
    assert missing.status_code == 404
    db_unavailable = client.get("/admin/briefs/503")
    assert db_unavailable.status_code == 503
    assert "Briefs temporarily unavailable" in db_unavailable.text
    assert "Could not load this brief from the database." in db_unavailable.text
    audit = client.get("/admin/audit")
    assert audit.status_code == 200
    assert "No audit events recorded yet." not in audit.text
    assert "audit-table" in audit.text
