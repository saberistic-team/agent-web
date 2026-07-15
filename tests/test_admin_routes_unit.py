"""Unit tests for admin CRM route handlers."""

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
from app.contacts import ContactRestoreResult
from app.admin_auth import SESSION_COOKIE_NAME
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

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
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/contacts/new", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1)}


@pytest.mark.unit
def test_create_contact_rejects_invalid_payload(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_companies.return_value = []
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            "/admin/contacts",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "full_name": "Pat Example",
                "company_id": "not-a-uuid",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


@pytest.mark.unit
def test_update_contact_rejects_invalid_payload(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_contact.return_value = {"id": CONTACT_ID, "full_name": "Pat"}
    crm.list_companies.return_value = []
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/edit",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "full_name": "Pat Example",
                "company_id": "not-a-uuid",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


@pytest.mark.unit
def test_restore_contact_redirects_to_edit(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.restore_contact.return_value = ContactRestoreResult(
        outcome="success",
        contact={"id": CONTACT_ID},
    )
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/restore",
            cookies=authenticated_admin["cookies"],
            data={"csrf_token": authenticated_admin["csrf_token"]},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/contacts/{CONTACT_ID}/edit"
    crm.restore_contact.assert_called_once()


@pytest.mark.unit
def test_companies_list_renders(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_companies.return_value = [{"id": COMPANY_ID, "name": "Acme", "status": "prospect"}]
    with patch("app.admin_routes._crm", crm):
        response = client.get("/admin/companies", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Companies" in response.text
    assert "Acme" in response.text


@pytest.mark.unit
def test_company_detail_returns_404_when_missing(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_company.return_value = None
    with patch("app.admin_routes._crm", crm):
        response = client.get(f"/admin/companies/{COMPANY_ID}", cookies=authenticated_admin["cookies"])
    assert response.status_code == 404


@pytest.mark.unit
def test_contact_edit_returns_404_when_missing(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_contact.return_value = None
    crm.list_companies.return_value = []
    with patch("app.admin_routes._crm", crm):
        response = client.get(
            f"/admin/contacts/{CONTACT_ID}/edit",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 404


@pytest.mark.unit
def test_update_contact_returns_404_when_missing(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.update_contact.return_value = None
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/edit",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "full_name": "Patricia",
                "company_id": str(COMPANY_ID),
            },
            follow_redirects=False,
        )
    assert response.status_code == 404
