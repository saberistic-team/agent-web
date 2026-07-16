"""Integration tests for admin contacts and company association (#105)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, admin_routes, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.contacts import ContactDuplicateWarning
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_session_store: dict[str, dict[str, Any]] = {}


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _session_row(*, token_hash: str, csrf_token_hash: str | None = None) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_token_hash,
    }


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
    _session_store[token_hash] = _session_row(
        token_hash=token_hash,
        csrf_token_hash=csrf_hash,
    )

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        row = _session_store.get(th)
        if row is None or row.get("revoked_at") is not None:
            return None
        return row

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
        with patch.object(admin_routes._crm, "list_contacts", return_value=[]):
            response = client.get("/admin/contacts", cookies=cookies)
        csrf_token = _extract_csrf_token(response.text)
        yield {"cookies": cookies, "csrf_token": csrf_token, "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_list_requires_auth() -> None:
    response = client.get("/admin/contacts")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_list_renders_for_admin(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(admin_routes._crm, "list_contacts", return_value=[]) as list_contacts:
        response = client.get("/admin/contacts", cookies=authenticated_admin["cookies"])

    assert response.status_code == 200
    assert "Contacts" in response.text
    assert 'href="/admin/contacts/new"' in response.text
    list_contacts.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_create_contact_assigns_roles_and_shows_duplicate_warnings(
    authenticated_admin: dict[str, Any],
) -> None:
    created = {
        "contact": {
            "id": CONTACT_ID,
            "full_name": "Pat Example",
            "buying_roles": ["founder", "technical_buyer"],
        },
        "duplicate_warnings": [
            ContactDuplicateWarning(
                contact_id=str(CONTACT_ID),
                label="Existing",
                match_type="profile_url",
            )
        ],
    }

    with patch.object(admin_routes._crm, "create_contact", return_value=created):
        response = client.post(
            "/admin/contacts",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "full_name": "Pat Example",
                "profile_url": "https://linkedin.com/in/pat/",
                "buying_roles": ["founder", "technical_buyer"],
            },
        )

    assert response.status_code == 303
    assert f"/admin/contacts/{CONTACT_ID}/edit" in response.headers["location"]
    assert "duplicate" in response.headers["location"].lower()


@pytest.mark.unit
@pytest.mark.integration
def test_archive_contact_redirects(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(
        admin_routes._crm, "archive_contact", return_value={"id": CONTACT_ID}
    ) as archive:
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/archive",
            cookies=authenticated_admin["cookies"],
            data={"csrf_token": authenticated_admin["csrf_token"]},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/contacts"
    archive.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_company_buying_group_requires_auth() -> None:
    response = client.get(f"/admin/companies/{COMPANY_ID}")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_company_page_shows_associated_contacts(authenticated_admin: dict[str, Any]) -> None:
    company = {
        "id": COMPANY_ID,
        "name": "Acme",
        "website": "https://acme.dev",
        "category": "fintech",
        "stage": "seed",
        "target_status": "target",
    }
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Pat Example",
            "title": "CTO",
            "buying_roles": ["technical_buyer"],
            "relationship_strength": "good",
        }
    ]

    with (
        patch.object(admin_routes._crm, "get_company", return_value=company),
        patch.object(admin_routes._crm, "list_contacts_for_company", return_value=contacts),
        patch.object(admin_routes._crm, "list_research_for_company", return_value=[]),
    ):
        response = client.get(
            f"/admin/companies/{COMPANY_ID}", cookies=authenticated_admin["cookies"]
        )

    assert response.status_code == 200
    assert "Buying-group coverage" in response.text
    assert "Pat Example" in response.text
    assert "Technical buyer" in response.text or "CTO" in response.text
    assert "Research gap" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_returns_503_without_database(
    monkeypatch: pytest.MonkeyPatch,
    authenticated_admin: dict[str, Any],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/contacts", cookies=authenticated_admin["cookies"])
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_new_form_renders(authenticated_admin: dict[str, Any]) -> None:
    with patch.object(
        admin_routes._crm,
        "list_companies",
        return_value=[{"id": COMPANY_ID, "name": "Acme"}],
    ):
        response = client.get(
            f"/admin/contacts/new?company_id={COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )

    assert response.status_code == 200
    assert "Add contact" in response.text
    assert str(COMPANY_ID) in response.text
