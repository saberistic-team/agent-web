"""Route tests for /admin/analytics dashboard and CSV export."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
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


def _session_row(*, token_hash: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "revoked_at": None,
    }


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_connection:
        db_connection.return_value.__enter__.return_value = conn
        db_connection.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_analytics_requires_auth() -> None:
    response = client.get("/admin/analytics")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
def test_analytics_preview_renders_populated_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin_auth

    enable_admin_preview_env(monkeypatch)
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(
                "/admin/analytics?days=30",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    body = response.text
    assert 'id="analytics-title"' in body
    assert "Funnel &amp; attribution" in body or "Marketing analytics" in body
    assert "Event volume" in body
    assert "Conversion rates" in body
    assert "Attribution" in body
    assert "Case study engagement" in body
    assert "Preview data — not production" in body
    assert "Landing Viewed" in body


@pytest.mark.unit
def test_analytics_export_preview_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import admin_auth

    enable_admin_preview_env(monkeypatch)
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ):
            response = client.get(
                "/admin/analytics/export.csv?days=7",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "conversion_rate" in response.text


@pytest.mark.unit
def test_analytics_db_error_shows_banner() -> None:
    from app import admin_auth

    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch(
            "app.admin_routes.db.get_admin_session_by_token_hash",
            return_value=row,
        ), patch(
            "app.admin_analytics_routes.load_analytics_dashboard",
            side_effect=RuntimeError("db down"),
        ):
            response = client.get(
                "/admin/analytics",
                cookies={SESSION_COOKIE_NAME: raw_token},
            )
    assert response.status_code == 200
    assert "temporarily unavailable" in response.text
