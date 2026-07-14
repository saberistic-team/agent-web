"""Tests for admin contact routes, roles, duplicates, archive, and authorization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_CONTACT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CSRF_TOKEN = "csrf-test-token"

_company = {"id": COMPANY_ID, "name": "Acme Corp", "status": "prospect"}
_contact = {
    "id": CONTACT_ID,
    "full_name": "Ada Lovelace",
    "title": "CTO",
    "email": "ada@acme.dev",
    "profile_url": "https://linkedin.com/in/ada",
    "company_id": COMPANY_ID,
    "email_provenance": "brief",
    "email_permission": "opt-in",
    "relationship_strength": 4,
    "last_interaction_at": datetime.now(timezone.utc),
    "notes": "Warm lead",
    "status": "active",
    "buying_roles": ["founder", "technical_buyer"],
}
_other_contact = {
    "id": OTHER_CONTACT_ID,
    "full_name": "Other Person",
    "email": "other@acme.dev",
    "company_id": COMPANY_ID,
    "status": "active",
    "buying_roles": ["influencer"],
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
    crm.get_company.return_value = _company
    crm.list_contacts.return_value = ([_contact, _other_contact], 2)
    crm.list_contacts_for_company_with_roles.return_value = [_contact, _other_contact]
    crm.get_contact.return_value = _contact
    crm.get_contact_with_roles.return_value = _contact
    crm.list_research_for_contact.return_value = []
    crm.find_contact_duplicates.return_value = []

    def create_contact(conn: Any, payload: Any) -> dict[str, Any]:
        return {**_contact, "full_name": payload.full_name, "buying_roles": payload.buying_roles}

    def update_contact(conn: Any, contact_id: UUID, payload: Any) -> dict[str, Any]:
        return {
            **_contact,
            "full_name": payload.full_name or _contact["full_name"],
            "buying_roles": payload.buying_roles or _contact["buying_roles"],
        }

    def archive_contact(conn: Any, contact_id: UUID) -> dict[str, Any]:
        return {**_contact, "status": "archived", "buying_roles": _contact["buying_roles"]}

    crm.create_contact.side_effect = create_contact
    crm.update_contact.side_effect = update_contact
    crm.archive_contact.side_effect = archive_contact

    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._issue_session_csrf", return_value=CSRF_TOKEN),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        yield crm


@pytest.mark.unit
def test_contacts_list_requires_authentication() -> None:
    response = client.get("/admin/contacts")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
def test_contacts_list_renders_contacts_when_authenticated() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get("/admin/contacts")
    assert response.status_code == 200
    assert "Ada Lovelace" in response.text
    assert "Technical buyer" in response.text
    assert 'href="/admin/contacts/new"' in response.text


@pytest.mark.unit
def test_contact_create_assigns_buying_roles(_mock_crm: MagicMock) -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={
                "csrf_token": CSRF_TOKEN,
                "full_name": "New Contact",
                "company_id": str(COMPANY_ID),
                "email": "new@acme.dev",
                "buying_roles": ["founder", "investor"],
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/contacts/{CONTACT_ID}"
    payload = _mock_crm.create_contact.call_args.args[1]
    assert payload.buying_roles == ["founder", "investor"]


@pytest.mark.unit
def test_contact_update_and_duplicate_warning_redirect(_mock_crm: MagicMock) -> None:
    from app.contacts import DuplicateWarning

    _mock_crm.find_contact_duplicates.return_value = [
        DuplicateWarning(reason="email", contact_id=str(OTHER_CONTACT_ID), label="Other")
    ]
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/edit",
            data={
                "csrf_token": CSRF_TOKEN,
                "full_name": "Ada Lovelace",
                "email": "ada@acme.dev",
                "buying_roles": ["technical_buyer"],
            },
        )
    assert response.status_code == 303
    assert "warn=email" in response.headers["location"]
    _mock_crm.update_contact.assert_called_once()


@pytest.mark.unit
def test_contact_archive_redirects_to_list(_mock_crm: MagicMock) -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/archive",
            data={"csrf_token": CSRF_TOKEN},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/contacts"
    _mock_crm.archive_contact.assert_called_once()


@pytest.mark.unit
def test_contact_detail_requires_authentication() -> None:
    response = client.get(f"/admin/contacts/{CONTACT_ID}")
    assert response.status_code == 303


@pytest.mark.unit
def test_contact_detail_shows_profile_and_roles() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/contacts/{CONTACT_ID}")
    assert response.status_code == 200
    assert "Ada Lovelace" in response.text
    assert "Founder" in response.text
    assert "Acme Corp" in response.text


@pytest.mark.unit
def test_contact_create_rejects_invalid_csrf() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={"csrf_token": "wrong", "full_name": "Bad"},
        )
    assert response.status_code == 400
