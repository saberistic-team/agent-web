"""Tests for admin contact management routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.contacts import ContactDuplicateMatch
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CSRF_TOKEN = "csrf-test-token"

_company = {"id": COMPANY_ID, "name": "Acme Corp", "status": "prospect"}
_contact = {
    "id": CONTACT_ID,
    "full_name": "Alex Ng",
    "email": "alex@acme.dev",
    "title": "CTO",
    "company_id": COMPANY_ID,
    "buying_roles": ["founder", "technical_buyer"],
    "display_name": "Alex Ng",
}


def _fake_session() -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=1,
        admin_username=TEST_USERNAME,
        token_hash="session-hash",
        csrf_token_hash=admin_auth.hash_csrf_token(CSRF_TOKEN),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.fixture(autouse=True)
def _mock_crm() -> Generator[MagicMock, None, None]:
    crm = MagicMock()
    crm.list_companies.return_value = [_company]
    crm.list_contacts.return_value = ([_contact], 1, MagicMock(include_archived=False))
    crm.get_contact.return_value = _contact
    crm.find_contact_duplicates.return_value = []
    crm.create_contact.return_value = _contact
    crm.update_contact.return_value = _contact
    crm.archive_contact.return_value = {**_contact, "archived_at": datetime.now(timezone.utc)}
    crm.list_contacts_for_company.return_value = [_contact]

    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._issue_session_csrf", return_value=CSRF_TOKEN),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        yield crm


@pytest.mark.unit
def test_contacts_route_requires_authentication() -> None:
    response = client.get("/admin/contacts")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
def test_contacts_route_lists_contacts_when_authenticated() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get("/admin/contacts")
    assert response.status_code == 200
    assert "Alex Ng" in response.text
    assert "Technical buyer" in response.text
    assert 'href="/admin/contacts/new"' in response.text


@pytest.mark.unit
def test_contact_new_form_renders_role_checkboxes() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get("/admin/contacts/new")
    assert response.status_code == 200
    assert 'name="buying_roles"' in response.text
    assert "Founder" in response.text
    assert "Email provenance" in response.text


@pytest.mark.unit
def test_contact_create_assigns_roles(_mock_crm: MagicMock) -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={
                "csrf_token": CSRF_TOKEN,
                "full_name": "Alex Ng",
                "email": "alex@acme.dev",
                "company_id": str(COMPANY_ID),
                "buying_roles": ["founder", "technical_buyer"],
            },
        )
    assert response.status_code == 303
    assert f"/admin/contacts/{CONTACT_ID}/edit" in response.headers["location"]
    _mock_crm.create_contact.assert_called_once()


@pytest.mark.unit
def test_contact_create_shows_duplicate_warning(_mock_crm: MagicMock) -> None:
    _mock_crm.find_contact_duplicates.return_value = [
        ContactDuplicateMatch(
            contact_id=CONTACT_ID,
            reason="matching email",
            contact=_contact,
        )
    ]
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={
                "csrf_token": CSRF_TOKEN,
                "full_name": "Alex Ng",
                "email": "alex@acme.dev",
            },
        )
    assert response.status_code == 409
    assert "Possible duplicates" in response.text
    _mock_crm.create_contact.assert_not_called()


@pytest.mark.unit
def test_contact_create_allows_confirmed_duplicates(_mock_crm: MagicMock) -> None:
    _mock_crm.find_contact_duplicates.return_value = [
        ContactDuplicateMatch(
            contact_id=CONTACT_ID,
            reason="matching email",
            contact=_contact,
        )
    ]
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={
                "csrf_token": CSRF_TOKEN,
                "full_name": "Alex Ng",
                "email": "alex@acme.dev",
                "confirm_duplicates": "true",
            },
        )
    assert response.status_code == 303
    _mock_crm.create_contact.assert_called_once()


@pytest.mark.unit
def test_contact_edit_renders_existing_roles() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/contacts/{CONTACT_ID}/edit")
    assert response.status_code == 200
    assert "Alex Ng" in response.text
    assert 'value="founder" checked' in response.text or 'value="founder"' in response.text


@pytest.mark.unit
def test_contact_archive_redirects_to_list(_mock_crm: MagicMock) -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/archive",
            data={"csrf_token": CSRF_TOKEN},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/contacts?notice=Contact%20archived"
    _mock_crm.archive_contact.assert_called_once()


@pytest.mark.unit
def test_contact_create_rejects_invalid_csrf() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={
                "csrf_token": "wrong-token",
                "full_name": "Alex Ng",
            },
        )
    assert response.status_code == 400


@pytest.mark.unit
def test_company_page_shows_associated_contacts_with_roles() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/companies/{COMPANY_ID}")
    assert response.status_code == 200
    assert "Alex Ng" in response.text
    assert "Technical buyer" in response.text
    assert f'href="/admin/contacts/{CONTACT_ID}/edit"' in response.text
