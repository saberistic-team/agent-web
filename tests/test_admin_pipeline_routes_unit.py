"""Unit tests for pipeline admin routes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.acquisition_pipeline import PipelineTransitionError
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> dict[str, Any]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in _session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_pipeline_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/pipeline", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_pipeline_redirects_to_login() -> None:
    response = client.get("/admin/pipeline")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_list_renders_for_authenticated_user(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_pipeline_companies.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Acme",
            "pipeline_stage": "qualified",
            "expected_value_cents": 50_000,
            "next_action": "Call founder",
            "next_action_due_at": datetime.now(timezone.utc),
            "pipeline_owner": "operator",
        }
    ]
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get("/admin/pipeline", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Acme" in response.text
    assert "Qualified" in response.text


@pytest.mark.unit
def test_pipeline_stage_change_requires_csrf(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        f"/admin/pipeline/{COMPANY_ID}/stage",
        data={"csrf_token": "bad", "to_stage": "qualified"},
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_stage_change_success_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "to_stage": "qualified",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/pipeline/{COMPANY_ID}"
    crm.transition_pipeline_stage.assert_called_once()


@pytest.mark.unit
def test_pipeline_stage_change_shows_transition_error(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.transition_pipeline_stage.side_effect = PipelineTransitionError("blocked")
    crm.get_pipeline_company.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
    }
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "to_stage": "won",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 400
    assert "blocked" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_preview_mode_returns_mock_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    response = client.get("/admin/pipeline")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Northwind Labs" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_detail_renders(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
    }
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get(
            f"/admin/pipeline/{COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Change stage" in response.text


@pytest.mark.unit
def test_pipeline_next_action_update_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "next_action": "Follow up",
                "expected_value_cents": "50000",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    crm.update_pipeline_next_action.assert_called_once()


@pytest.mark.unit
def test_pipeline_activity_post_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/activities",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "activity_type": "note",
                "summary": "Left voicemail",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    crm.record_pipeline_activity.assert_called_once()


@pytest.mark.unit
def test_pipeline_preview_detail_fixed_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    company_id = PREVIEW_PIPELINE_COMPANY_IDS[0]
    response = client.get(f"/admin/pipeline/{company_id}")
    assert response.status_code == 200
    assert "Stage history" in response.text
