"""Unit tests for qualification target admin routes."""

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
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01")

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
        patch("app.admin_qualification_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/targets", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_targets_redirects_to_login() -> None:
    response = client.get("/admin/targets")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_targets_list_renders_for_authenticated_user(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_qualification_targets.return_value = [
        {
            "company_id": str(COMPANY_ID),
            "id": COMPANY_ID,
            "name": "Northwind Labs",
            "score": 9,
            "tier": "A",
            "stage": "seed",
            "vertical": "fintech",
            "strongest_signals": ["Target vertical"],
            "warm_path": "Sam Intro",
            "has_warm_path": True,
            "next_action": "Review evidence",
            "evidence_freshness": "fresh",
            "missing_fields": [],
            "pipeline_stage": "qualified",
            "pipeline_owner": "alex",
            "stale_evidence": False,
        }
    ]
    crm.list_qualification_working_lists.return_value = []
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get("/admin/targets", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Northwind Labs" in response.text
    assert "Target lists" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_targets_list_applies_filters(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_qualification_targets.return_value = []
    crm.list_qualification_working_lists.return_value = []
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get(
            "/admin/targets?tier=A&category=fintech&warm_path=yes",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    kwargs = crm.list_qualification_targets.call_args.kwargs
    assert kwargs["filters"].tier == "A"
    assert kwargs["filters"].category == "fintech"
    assert kwargs["filters"].warm_path == "yes"


@pytest.mark.unit
@pytest.mark.integration
def test_targets_list_ignores_invalid_filters(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_qualification_targets.return_value = []
    crm.list_qualification_working_lists.return_value = []
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get(
            "/admin/targets?tier=Z",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    kwargs = crm.list_qualification_targets.call_args.kwargs
    assert kwargs["filters"] is None


@pytest.mark.unit
@pytest.mark.integration
def test_target_detail_renders(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_company.return_value = {"id": COMPANY_ID, "name": "Northwind Labs"}
    crm.list_qualification_targets.return_value = [
        {
            "company_id": str(COMPANY_ID),
            "tier": "A",
            "score": 9,
            "strongest_signals": [],
            "warm_path": None,
            "evidence_freshness": "fresh",
            "missing_fields": [],
            "pipeline_stage": "qualified",
            "stale_evidence": False,
        }
    ]
    crm.list_qualification_tier_history.return_value = [
        {
            "changed_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "from_tier": "B",
            "to_tier": "A",
            "score": 9,
            "changed_by": "operator",
        }
    ]
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get(
            f"/admin/targets/{COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Tier changes" in response.text
    assert "Northwind Labs" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_target_detail_404_for_missing_company(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_company.return_value = None
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get(
            f"/admin/targets/{COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.integration
def test_save_working_list_requires_csrf(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        "/admin/targets/working-list",
        data={"csrf_token": "bad", "name": "Shortlist", "company_ids": [str(COMPANY_ID)]},
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_save_working_list_success_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.post(
            "/admin/targets/working-list",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "name": "Q3 shortlist",
                "company_ids": [str(COMPANY_ID)],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/targets?saved=1"
    crm.save_qualification_working_list.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_targets_preview_mode_returns_mock_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get("/admin/targets")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Target lists" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_target_detail_preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get(f"/admin/targets/{COMPANY_ID}")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_save_working_list_preview_mode_rejects_unsafe_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Central AdminPreviewReadOnlyMiddleware (#331) rejects every unsafe method
    # under /admin with 405 before the route handler runs.
    enable_admin_preview_env(monkeypatch)
    with patch("app.admin_routes._verify_session_csrf"):
        response = client.post(
            "/admin/targets/working-list",
            data={
                "csrf_token": "irrelevant-in-preview",
                "name": "Preview list",
                "company_ids": [str(COMPANY_ID)],
            },
        )
    assert response.status_code == 405


@pytest.mark.unit
@pytest.mark.integration
def test_targets_list_handles_database_errors(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_qualification_targets.side_effect = RuntimeError("db down")
    crm.list_qualification_working_lists.return_value = []
    with patch("app.admin_qualification_routes._crm", crm):
        response = client.get("/admin/targets", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "No active targets match these filters" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_target_detail_503_without_database(
    authenticated_admin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    response = client.get(
        f"/admin/targets/{COMPANY_ID}",
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_save_working_list_invalid_payload_redirects(authenticated_admin: dict[str, Any]) -> None:
    from app.qualification_targets import MAX_WORKING_LIST_ITEMS

    with patch("app.admin_qualification_routes._crm", MagicMock()):
        response = client.post(
            "/admin/targets/working-list",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "name": "Too big",
                "company_ids": [str(UUID(int=i)) for i in range(MAX_WORKING_LIST_ITEMS + 1)],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/targets"


@pytest.mark.unit
@pytest.mark.integration
def test_save_working_list_503_without_database(
    authenticated_admin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    response = client.post(
        "/admin/targets/working-list",
        data={
            "csrf_token": authenticated_admin["csrf_token"],
            "name": "Shortlist",
            "company_ids": [str(COMPANY_ID)],
        },
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_target_detail_preview_404_for_unknown_id(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get(
        "/admin/targets/00000000-0000-0000-0000-000000000099",
    )
    assert response.status_code == 404
