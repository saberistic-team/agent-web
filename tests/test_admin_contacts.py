"""Tests for admin contact CRUD routes, authorization, and duplicate warnings."""

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

pytestmark = [pytest.mark.unit, pytest.mark.integration]

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
    "full_name": "Ada Lovelace",
    "title": "CTO",
    "email": "ada@acme.dev",
    "company_id": COMPANY_ID,
    "buying_roles": ["technical_buyer"],
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
    crm.list_contacts.return_value = [_contact]
    crm.get_company.return_value = _company
    crm.get_contact.return_value = _contact
    crm.list_contacts_for_company.return_value = [_contact]
    crm.list_research_for_company.return_value = []
    crm.create_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
    crm.update_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
    crm.archive_contact.return_value = _contact
    crm.restore_contact.return_value = _contact

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
    assert "Ada Lovelace" in response.text
    assert 'class="admin-app"' in response.text


@pytest.mark.unit
def test_contact_record_mutations_require_session_and_use_csrf() -> None:
    unauthenticated = client.post(
        "/admin/contacts",
        data={"csrf_token": CSRF_TOKEN, "full_name": "Ada", "company_id": str(COMPANY_ID)},
    )
    assert unauthenticated.status_code == 303

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            crm.create_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
            response = client.post(
                "/admin/contacts",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "full_name": "Ada Lovelace",
                    "company_id": str(COMPANY_ID),
                    "buying_roles": ["founder", "technical_buyer"],
                    "email": "ada@acme.dev",
                    "email_permission": "permitted",
                },
            )
            assert response.status_code == 303
            assert f"/admin/contacts/{CONTACT_ID}/edit" in response.headers["location"]
            created = crm.create_contact.call_args.kwargs["contact"]
            assert created.buying_roles == ["founder", "technical_buyer"]
            assert created.email_permission == "permitted"
            client.post(
                f"/admin/contacts/{CONTACT_ID}/archive",
                data={"csrf_token": CSRF_TOKEN},
            )
            crm.archive_contact.assert_called_once()


@pytest.mark.unit
def test_contact_new_edit_restore_and_invalid_fields_are_handled() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            crm.get_contact.return_value = _contact
            crm.update_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
            crm.restore_contact.return_value = _contact

            new_page = client.get("/admin/contacts/new")
            assert new_page.status_code == 200 and "Add contact" in new_page.text

            bad = client.post(
                "/admin/contacts",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "full_name": "Ada",
                    "company_id": str(COMPANY_ID),
                    "relationship_strength": "invalid",
                },
            )
            assert bad.status_code == 303
            assert "error=" in bad.headers["location"]

            edit_page = client.get(f"/admin/contacts/{CONTACT_ID}/edit")
            assert edit_page.status_code == 200 and "Ada Lovelace" in edit_page.text

            updated = client.post(
                f"/admin/contacts/{CONTACT_ID}/edit",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "full_name": "Ada Lovelace",
                    "company_id": str(COMPANY_ID),
                    "buying_roles": ["investor"],
                },
            )
            assert updated.status_code == 303
            assert crm.update_contact.call_args.kwargs["contact"].buying_roles == ["investor"]

            restored = client.post(
                f"/admin/contacts/{CONTACT_ID}/restore",
                data={"csrf_token": CSRF_TOKEN},
            )
            assert restored.status_code == 303
            crm.restore_contact.assert_called_once()


@pytest.mark.unit
def test_contact_edit_requires_existing_contact() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            crm.get_contact.return_value = None
            response = client.get(f"/admin/contacts/{CONTACT_ID}/edit")
    assert response.status_code == 404


@pytest.mark.unit
def test_company_research_page_shows_associated_contacts() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/companies/{COMPANY_ID}")
    assert response.status_code == 200
    assert "Ada Lovelace" in response.text
    assert "Technical buyer" in response.text
