"""Unit tests for daily action queue admin routes."""

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
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
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
        patch("app.admin_action_queue_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/queue", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_queue_redirects_to_login() -> None:
    response = client.get("/admin/queue")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_queue_list_renders_for_authenticated_user(authenticated_admin: dict[str, Any]) -> None:
    with patch("app.admin_action_queue_routes.load_action_queue") as load_queue:
        from app.acquisition_action_queue import ActionQueueData, ActionQueueItem

        load_queue.return_value = ActionQueueData(
            items=(
                ActionQueueItem(
                    item_key="overdue:1",
                    priority_rank=1,
                    category="overdue_action",
                    reason="Overdue next action for Acme.",
                    company_id=str(COMPANY_ID),
                    company_name="Acme",
                    next_action="Call founder",
                    next_action_due_at=datetime.now(timezone.utc),
                    pipeline_stage="qualified",
                ),
            ),
            generated_at=datetime.now(timezone.utc),
        )
        response = client.get("/admin/queue", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Daily action queue" in response.text
    assert "Acme" in response.text
    assert "Overdue next action" in response.text
    assert "Export spreadsheet" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_queue_complete_requires_csrf(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        "/admin/queue/complete",
        data={
            "company_id": str(COMPANY_ID),
            "item_key": "overdue:1",
            "item_category": "overdue_action",
            "csrf_token": "wrong",
        },
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_queue_complete_records_action(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_action_queue_routes._crm", crm):
        response = client.post(
            "/admin/queue/complete",
            data={
                "company_id": str(COMPANY_ID),
                "item_key": "overdue:1",
                "item_category": "overdue_action",
                "csrf_token": authenticated_admin["csrf_token"],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert "/admin/queue" in response.headers["location"]
    crm.complete_queue_item.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_queue_export_requires_auth() -> None:
    response = client.get("/admin/queue/export.csv")
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_queue_export_returns_csv(authenticated_admin: dict[str, Any]) -> None:
    with (
        patch("app.admin_action_queue_routes._crm") as crm,
        patch("app.admin_action_queue_routes.render_acquisition_export_csv") as render_csv,
    ):
        crm.request_export.return_value = {"export_type": "acquisition_queue_csv"}
        render_csv.return_value = "company_name,tier\nAcme,A\n"
        response = client.get(
            "/admin/queue/export.csv",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "text/csv" in response.headers.get("content-type", "")
    assert "Acme" in response.text
    crm.request_export.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_queue_snooze_calls_service(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_action_queue_routes._crm", crm):
        response = client.post(
            "/admin/queue/snooze",
            data={
                "company_id": str(COMPANY_ID),
                "item_key": "overdue:1",
                "item_category": "overdue_action",
                "snooze_days": "7",
                "csrf_token": authenticated_admin["csrf_token"],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    crm.snooze_queue_item.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_preview_queue_renders_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/queue")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Daily action queue" in response.text
    assert "Tier A qualified" in response.text
    assert "Warm introduction" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_queue_db_error_shows_banner(authenticated_admin: dict[str, Any]) -> None:
    with patch("app.admin_action_queue_routes.load_action_queue", side_effect=RuntimeError("db down")):
        response = client.get("/admin/queue", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Action queue is temporarily unavailable" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_queue_reschedule_calls_service(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    due = "2026-07-20T15:00:00+00:00"
    with patch("app.admin_action_queue_routes._crm", crm):
        response = client.post(
            "/admin/queue/reschedule",
            data={
                "company_id": str(COMPANY_ID),
                "item_key": "overdue:1",
                "item_category": "overdue_action",
                "next_action_due_at": due,
                "csrf_token": authenticated_admin["csrf_token"],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert "Rescheduled" in response.headers["location"]
    crm.reschedule_queue_item.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_queue_reschedule_rejects_invalid_due_date(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        "/admin/queue/reschedule",
        data={
            "company_id": str(COMPANY_ID),
            "item_key": "overdue:1",
            "item_category": "overdue_action",
            "next_action_due_at": "   ",
            "csrf_token": authenticated_admin["csrf_token"],
        },
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_queue_replace_calls_service(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_action_queue_routes._crm", crm):
        response = client.post(
            "/admin/queue/replace",
            data={
                "company_id": str(COMPANY_ID),
                "item_key": "overdue:1",
                "item_category": "overdue_action",
                "next_action": "Send intro email",
                "next_action_due_at": "2026-07-21T10:00:00+00:00",
                "csrf_token": authenticated_admin["csrf_token"],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    crm.replace_queue_item.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_queue_replace_rejects_empty_action(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        "/admin/queue/replace",
        data={
            "company_id": str(COMPANY_ID),
            "item_key": "overdue:1",
            "item_category": "overdue_action",
            "next_action": "",
            "next_action_due_at": "2026-07-21T10:00:00+00:00",
            "csrf_token": authenticated_admin["csrf_token"],
        },
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.integration
def test_preview_export_returns_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    with patch.object(db, "get_admin_session_by_token_hash", side_effect=lambda _c, th: _session_store.get(th)):
        response = client.get(
            "/admin/queue/export.csv",
            cookies={SESSION_COOKIE_NAME: raw_token},
        )
    assert response.status_code == 200
    assert "company_name" in response.text
    assert "'=" in response.text or "Acme" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_queue_actions_reject_unsafe_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Central AdminPreviewReadOnlyMiddleware (#331) rejects every unsafe method
    # under /admin with 405 before the route handler runs.
    enable_admin_preview_env(monkeypatch)
    with patch("app.admin_routes._verify_session_csrf"):
        for path, extra in (
            ("/admin/queue/complete", {}),
            ("/admin/queue/snooze", {"snooze_days": "3"}),
            ("/admin/queue/reschedule", {"next_action_due_at": "2026-07-21T10:00:00+00:00"}),
            (
                "/admin/queue/replace",
                {
                    "next_action": "New task",
                    "next_action_due_at": "2026-07-21T10:00:00+00:00",
                },
            ),
        ):
            data = {
                "company_id": str(COMPANY_ID),
                "item_key": "overdue:1",
                "item_category": "overdue_action",
                "csrf_token": "irrelevant-in-preview",
                **extra,
            }
            response = client.post(path, data=data)
            assert response.status_code == 405, path
