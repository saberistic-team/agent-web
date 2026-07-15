"""Tests for admin contact CRUD routes, authorization, and archive behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.contacts import ContactDuplicateWarning
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
    "full_name": "Alice Example",
    "email": "alice@acme.dev",
    "company_id": COMPANY_ID,
    "buying_roles": ["founder"],
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
    crm.list_contacts.return_value = [{**_contact, "company_name": "Acme Corp"}]
    crm.get_contact.return_value = _contact
    crm.create_contact.return_value = {
        "contact": _contact,
        "duplicate_warnings": [
            ContactDuplicateWarning(
                contact_id=str(CONTACT_ID),
                full_name="Other Alice",
                reason="email",
            )
        ],
    }
    crm.update_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
    crm.archive_contact.return_value = {**_contact, "archived_at": datetime.now(timezone.utc)}

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
    assert "Alice Example" in response.text
    assert "Add contact" in response.text


@pytest.mark.unit
def test_contact_mutations_require_session_and_use_csrf() -> None:
    unauthenticated = client.post(
        "/admin/contacts",
        data={"csrf_token": CSRF_TOKEN, "full_name": "Alice"},
    )
    assert unauthenticated.status_code == 303

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            crm.create_contact.return_value = {"contact": _contact, "duplicate_warnings": []}
            response = client.post(
                "/admin/contacts",
                data={
                    "csrf_token": CSRF_TOKEN,
                    "full_name": "Alice Example",
                    "company_id": str(COMPANY_ID),
                    "buying_roles": ["founder", "technical_buyer"],
                    "email": "alice@acme.dev",
                    "email_permission": "permitted",
                },
            )
            assert response.status_code == 303
            assert f"/admin/contacts/{CONTACT_ID}/edit" in response.headers["location"]
            created = crm.create_contact.call_args.kwargs["contact"]
            assert created.buying_roles == ["founder", "technical_buyer"]

            client.post(
                f"/admin/contacts/{CONTACT_ID}/archive",
                data={"csrf_token": CSRF_TOKEN},
            )
            crm.archive_contact.assert_called_once()


@pytest.mark.unit
def test_contact_edit_rejects_invalid_csrf() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/edit",
            data={"csrf_token": "wrong", "full_name": "Alice"},
        )
    assert response.status_code == 400


@pytest.mark.unit
def test_contact_create_redirects_with_duplicate_warning_count() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            "/admin/contacts",
            data={"csrf_token": CSRF_TOKEN, "full_name": "Alice Example"},
        )
    assert response.status_code == 303
    assert "warning=1%20possible%20duplicate" in response.headers["location"]


@pytest.mark.unit
def test_contact_detail_and_edit_render_contact_identity() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        detail = client.get(f"/admin/contacts/{CONTACT_ID}")
        edit = client.get(f"/admin/contacts/{CONTACT_ID}/edit")
    assert detail.status_code == 200
    assert "Alice Example" in detail.text
    assert edit.status_code == 200
    assert "Edit Alice Example" in edit.text
