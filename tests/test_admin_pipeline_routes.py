"""Tests for authenticated admin pipeline API routes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@contextmanager
def admin_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    yield


@contextmanager
def mock_db_conn() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_pipeline_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _login(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("app.admin_routes.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        with patch("app.admin_routes.db.create_admin_login_flow") as create_flow:
            with patch("app.admin_routes.db.get_admin_login_flow_by_token_hash") as get_flow:
                with patch("app.admin_routes.db.consume_admin_login_flow"):
                    with patch("app.admin_routes.db.create_admin_session"):
                        with patch("app.admin_routes.admin_auth.is_login_throttled", return_value=False):
                            create_flow.return_value = 1
                            get_flow.return_value = {
                                "csrf_token_hash": __import__(
                                    "app.admin_auth", fromlist=["hash_csrf_token"]
                                ).admin_auth.hash_csrf_token("csrf-token"),
                                "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
                                "consumed_at": None,
                            }
                            login_page = client.get("/admin/login")
                            csrf_match = __import__("re").search(
                                r'name="csrf_token" value="([^"]+)"',
                                login_page.text,
                            )
                            csrf_token = csrf_match.group(1) if csrf_match else "csrf-token"
                            response = client.post(
                                "/admin/login",
                                data={
                                    "username": TEST_USERNAME,
                                    "password": TEST_PASSWORD,
                                    "csrf_token": csrf_token,
                                },
                            )
    assert response.status_code == 303


@pytest.mark.unit
def test_pipeline_routes_require_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        response = client.get("/admin/api/pipeline/stages")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
def test_list_pipeline_stages_when_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=1)
            response = client.get("/admin/api/pipeline/stages")
    assert response.status_code == 200
    payload = response.json()
    assert "researching" in payload["stages"]
    assert "larger_engagement" in payload["stages"]


@pytest.mark.unit
def test_transition_stage_returns_confirm_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.crm_service import ConfirmRequiredError

    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.transition_company_stage.side_effect = (
                        ConfirmRequiredError("needs confirm")
                    )
                    response = client.post(
                        f"/admin/api/pipeline/companies/{COMPANY_ID}/stage",
                        json={"to_stage": "contacted"},
                    )
    assert response.status_code == 409
    assert "needs confirm" in response.json()["detail"]


@pytest.mark.unit
def test_transition_stage_success(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.transition_company_stage.return_value = {
                        "company": {"id": str(COMPANY_ID), "pipeline_stage": "qualified"},
                        "history": {"from_stage": "researching", "to_stage": "qualified"},
                    }
                    response = client.post(
                        f"/admin/api/pipeline/companies/{COMPANY_ID}/stage",
                        json={"to_stage": "qualified"},
                    )
    assert response.status_code == 200
    assert response.json()["company"]["pipeline_stage"] == "qualified"


@pytest.mark.unit
def test_list_overdue_actions_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.list_overdue_actions.return_value = [
                        {
                            "id": str(COMPANY_ID),
                            "name": "Acme",
                            "next_action": "Follow up",
                        }
                    ]
                    response = client.get("/admin/api/pipeline/actions/overdue")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["companies"]) == 1
    assert "as_of" in payload


@pytest.mark.unit
def test_record_activity_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.record_activity_for_company.return_value = {
                        "id": "act-1",
                        "activity_type": "proposal",
                        "summary": "Sent diagnostic proposal",
                    }
                    response = client.post(
                        f"/admin/api/pipeline/companies/{COMPANY_ID}/activities",
                        json={
                            "activity_type": "proposal",
                            "summary": "Sent diagnostic proposal",
                        },
                    )
    assert response.status_code == 200
    assert response.json()["activity"]["activity_type"] == "proposal"


@pytest.mark.unit
def test_list_companies_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.list_companies_by_stage.return_value = [
                        {"id": str(COMPANY_ID), "pipeline_stage": "qualified"}
                    ]
                    response = client.get("/admin/api/pipeline/companies?stage=qualified")
    assert response.status_code == 200
    assert response.json()["companies"][0]["pipeline_stage"] == "qualified"


@pytest.mark.unit
def test_get_company_detail_route_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.get_company_pipeline_detail.return_value = None
                    response = client.get(f"/admin/api/pipeline/companies/{COMPANY_ID}")
    assert response.status_code == 404


@pytest.mark.unit
def test_update_next_action_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.update_company_next_action.return_value = {
                        "id": str(COMPANY_ID),
                        "next_action": "Send proposal",
                    }
                    response = client.patch(
                        f"/admin/api/pipeline/companies/{COMPANY_ID}/next-action",
                        json={"next_action": "Send proposal"},
                    )
    assert response.status_code == 200
    assert response.json()["company"]["next_action"] == "Send proposal"


@pytest.mark.unit
def test_list_upcoming_actions_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            with mock_db_conn():
                with patch("app.admin_pipeline_routes._crm_service") as crm_service:
                    crm_service.return_value.list_upcoming_actions.return_value = [
                        {"id": str(COMPANY_ID), "next_action": "Call"}
                    ]
                    response = client.get("/admin/api/pipeline/actions/upcoming?within_days=3")
    assert response.status_code == 200
    payload = response.json()
    assert payload["within_days"] == 3
    assert len(payload["companies"]) == 1


@pytest.mark.unit
def test_list_activity_types_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with admin_env(monkeypatch):
        with patch("app.admin_pipeline_routes.require_admin_session") as require_session:
            require_session.return_value = MagicMock(admin_username=TEST_USERNAME, id=0)
            response = client.get("/admin/api/pipeline/activity-types")
    assert response.status_code == 200
    assert "task_completion" in response.json()["activity_types"]
