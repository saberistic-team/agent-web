"""Tests for the acquisition admin dashboard (#108)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.acquisition_dashboard import (
    AcquisitionDashboardData,
    CompanyAttentionRow,
    CountBucket,
    EvidenceRow,
    NextActionRow,
)
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_dashboard_pages import render_acquisition_dashboard_page
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"
COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
    }


def _empty_dashboard() -> AcquisitionDashboardData:
    return AcquisitionDashboardData(
        company_counts_by_stage=(),
        company_counts_by_category=(),
        contact_counts_by_stage=(),
        contact_counts_by_category=(),
        overdue_actions=(),
        upcoming_actions=(),
        recent_evidence=(),
        stale_evidence=(),
        without_decision_maker=(),
        without_next_action=(),
        generated_at=NOW,
    )


def _populated_dashboard() -> AcquisitionDashboardData:
    return AcquisitionDashboardData(
        company_counts_by_stage=(CountBucket(key="seed", label="Seed", count=3),),
        company_counts_by_category=(CountBucket(key="fintech", label="Fintech", count=2),),
        contact_counts_by_stage=(CountBucket(key="seed", label="Seed", count=5),),
        contact_counts_by_category=(CountBucket(key="fintech", label="Fintech", count=4),),
        overdue_actions=(
            NextActionRow(
                company_id=str(COMPANY_ID),
                company_name="Northwind Labs",
                pipeline_stage="qualified",
                pipeline_owner="Alex Nguyen",
                next_action="Schedule architecture review",
                next_action_due_at=NOW - timedelta(days=2),
            ),
        ),
        upcoming_actions=(
            NextActionRow(
                company_id=str(COMPANY_ID),
                company_name="Helios Rail",
                pipeline_stage="contacted",
                pipeline_owner="Sam Patel",
                next_action="Send follow-up deck",
                next_action_due_at=NOW + timedelta(days=3),
            ),
        ),
        recent_evidence=(
            EvidenceRow(
                record_id="e1",
                company_id=str(COMPANY_ID),
                company_name="Cedar Protocol",
                record_type="verified_fact",
                body="Raised Series A",
                created_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=30),
            ),
        ),
        stale_evidence=(
            EvidenceRow(
                record_id="e2",
                company_id=str(COMPANY_ID),
                company_name="Aperture Freight",
                record_type="public_signal",
                body="Hiring spike",
                created_at=NOW - timedelta(days=40),
                expires_at=NOW - timedelta(days=1),
            ),
        ),
        without_decision_maker=(
            CompanyAttentionRow(
                company_id=str(COMPANY_ID),
                company_name="Meridian Stack",
                target_status="target",
                category="fintech",
                stage="seed",
            ),
        ),
        without_next_action=(
            CompanyAttentionRow(
                company_id=str(COMPANY_ID),
                company_name="Volt Spiral",
                target_status="watching",
                category="ai_infrastructure",
                stage="series_a",
                pipeline_stage="researching",
            ),
        ),
        generated_at=NOW,
    )


@pytest.mark.unit
def test_render_empty_dashboard_has_actionable_links() -> None:
    html = render_acquisition_dashboard_page(
        data=_empty_dashboard(),
        admin_username=TEST_USERNAME,
    )
    assert "Start building your pipeline" in html
    assert 'href="/admin/companies/new"' in html
    assert "/admin/imports" in html
    assert "/admin/discovery" in html


@pytest.mark.unit
def test_render_populated_dashboard_includes_metric_sections() -> None:
    html = render_acquisition_dashboard_page(
        data=_populated_dashboard(),
        admin_username=TEST_USERNAME,
    )
    assert "Today&apos;s attention" in html
    assert "Overdue next actions" in html
    assert "Northwind Labs" in html
    assert "Recently added evidence" in html
    assert "Stale evidence" in html
    assert "Missing decision-maker" in html
    assert "qualifying decision-maker" in html
    assert "Meridian Stack" in html
    assert "Schedule architecture review" in html
    assert "/admin/pipeline/" in html


@pytest.mark.unit
def test_render_dashboard_formats_naive_datetimes() -> None:
    naive = datetime(2026, 7, 15, 8, 30)
    data = AcquisitionDashboardData(
        company_counts_by_stage=(CountBucket(key="seed", label="Seed", count=1),),
        company_counts_by_category=(),
        contact_counts_by_stage=(),
        contact_counts_by_category=(),
        overdue_actions=(
            NextActionRow(
                company_id=str(COMPANY_ID),
                company_name="Naive Co",
                pipeline_stage="qualified",
                pipeline_owner=None,
                next_action="Follow up",
                next_action_due_at=naive,
            ),
        ),
        upcoming_actions=(),
        recent_evidence=(),
        stale_evidence=(),
        without_decision_maker=(),
        without_next_action=(),
        generated_at=naive,
    )
    html = render_acquisition_dashboard_page(
        data=data,
        admin_username=TEST_USERNAME,
        preview_banner="Preview data — not production",
    )
    assert "Preview data — not production" in html
    assert "2026-07-15 08:30 UTC" in html


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_requires_auth() -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_empty_state() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.load_acquisition_dashboard",
                return_value=_empty_dashboard(),
            ),
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert "Start building your pipeline" in response.text
    assert 'href="/admin/companies/new"' in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_populated_state() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.load_acquisition_dashboard",
                return_value=_populated_dashboard(),
            ),
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert "Overdue next actions" in response.text
    assert "Northwind Labs" in response.text
    assert "Meridian Stack" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_admin_dashboard_db_error_banner() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_routes.load_acquisition_dashboard",
                side_effect=RuntimeError("db down"),
            ),
        ):
            response = client.get("/admin", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    assert "temporarily unavailable" in response.text
