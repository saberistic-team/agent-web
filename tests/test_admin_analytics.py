"""Integration tests for the admin marketing analytics dashboard (#116)."""

from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app
from app.marketing_analytics_dashboard import (
    empty_dashboard_data,
    parse_analytics_date_range,
)
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


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
    now = datetime.now(timezone.utc)
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "revoked_at": None,
    }


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code in {302, 303, 307}


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_renders_dashboard() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    date_range = parse_analytics_date_range()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=empty_dashboard_data(date_range),
            ),
            patch("app.admin_analytics_routes.db.db_connection") as analytics_db,
        ):
            analytics_db.return_value.__enter__.return_value = MagicMock()
            analytics_db.return_value.__exit__.return_value = None
            response = client.get("/admin/analytics", cookies={SESSION_COOKIE_NAME: raw_token})
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title"' in body
    assert "Funnel &amp; attribution" in body or "Funnel & attribution" in body
    assert "Conversion rates" in body
    assert "Case study engagement" in body
    assert "Export CSV" in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_preview_mode_has_mock_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    response = client.get(
        "/admin/analytics",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Preview data" in body
    assert "Lead Persisted" in body or "Leads persisted" in body
    assert "Conversion rates" in body
    assert "No events in this range" not in body


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_requires_auth() -> None:
    response = client.get("/admin/analytics/export.csv")
    assert response.status_code in {302, 303, 307}


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_aggregated() -> None:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    date_range = parse_analytics_date_range()
    with mock_db_connection():
        with (
            patch(
                "app.admin_routes.db.get_admin_session_by_token_hash",
                return_value=row,
            ),
            patch(
                "app.admin_analytics_routes.load_marketing_analytics_dashboard",
                return_value=empty_dashboard_data(date_range),
            ),
            patch("app.admin_analytics_routes.db.db_connection") as analytics_db,
        ):
            analytics_db.return_value.__enter__.return_value = MagicMock()
            analytics_db.return_value.__exit__.return_value = None
            response = client.get(
                "/admin/analytics/export.csv",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "Marketing analytics export" in body
    assert "anonymous_session" not in body.lower()


@pytest.mark.unit
@pytest.mark.integration
def test_admin_analytics_export_csv_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    response = client.get(
        "/admin/analytics/export.csv",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    assert "Marketing analytics export" in response.text


@pytest.mark.unit
def test_preview_marketing_analytics_is_deterministic() -> None:
    from app.admin_preview import build_preview_marketing_analytics_data

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    a = build_preview_marketing_analytics_data(rng=random.Random(42), now=now)
    b = build_preview_marketing_analytics_data(rng=random.Random(42), now=now)
    assert a == b
    assert a.event_counts
    assert a.attribution
    assert a.case_study_engagement
