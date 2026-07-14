"""Tests for admin research record routes, authorization, and XSS safety."""

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
CSRF_TOKEN = "csrf-test-token"

_company = {
    "id": COMPANY_ID,
    "name": "Acme Corp",
    "status": "prospect",
}
_contact = {
    "id": CONTACT_ID,
    "email": "lead@acme.dev",
    "company_id": COMPANY_ID,
}
_records: list[dict[str, Any]] = []


def _fake_session() -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=1,
        admin_username=TEST_USERNAME,
        token_hash="session-hash",
        csrf_token_hash=admin_auth.hash_csrf_token(CSRF_TOKEN),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _reset_records() -> Generator[None, None, None]:
    _records.clear()
    yield
    _records.clear()


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
    crm.list_contacts_for_company.return_value = [_contact]
    crm.get_contact.return_value = _contact
    crm.list_research_for_company.side_effect = lambda conn, company_id, **kw: list(_records)
    crm.list_research_for_contact.side_effect = lambda conn, contact_id, **kw: list(_records)

    def attach_research_record(conn: Any, **kwargs: Any) -> dict[str, Any]:
        record = {
            "id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            "record_type": kwargs["record_type"],
            "body": kwargs["body"],
            "company_id": kwargs["company_id"],
            "contact_id": kwargs.get("contact_id"),
            "source_name": kwargs.get("source_name"),
            "source_url": kwargs.get("source_url"),
            "observed_value": kwargs.get("observed_value"),
            "observed_at": kwargs.get("observed_at"),
            "confidence": kwargs.get("confidence"),
            "review_at": kwargs.get("review_at"),
            "expires_at": kwargs.get("expires_at"),
        }
        _records.append(record)
        return record

    crm.attach_research_record.side_effect = attach_research_record

    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._issue_session_csrf", return_value=CSRF_TOKEN),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        yield crm


@pytest.mark.unit
def test_companies_route_requires_authentication() -> None:
    response = client.get("/admin/companies")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
def test_companies_route_lists_companies_when_authenticated() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get("/admin/companies")
    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert 'class="admin-app"' in response.text


@pytest.mark.unit
def test_company_research_create_appends_conflicting_observations() -> None:
    now = datetime.now(timezone.utc)
    evidence = {
        "record_type": "verified_fact",
        "body": "Headcount stable",
        "source_name": "LinkedIn",
        "source_url": "https://linkedin.com/company/acme",
        "observed_value": "120 employees",
        "observed_at": now.isoformat(),
        "confidence": "0.9",
        "review_at": (now + timedelta(days=30)).isoformat(),
        "expires_at": (now + timedelta(days=60)).isoformat(),
    }
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        first = client.post(
            f"/admin/companies/{COMPANY_ID}/research",
            data={"csrf_token": CSRF_TOKEN, **evidence},
        )
        conflicting = {**evidence, "observed_value": "180 employees"}
        second = client.post(
            f"/admin/companies/{COMPANY_ID}/research",
            data={"csrf_token": CSRF_TOKEN, **conflicting},
        )
    assert first.status_code == 303
    assert second.status_code == 303
    assert len(_records) == 2
    assert _records[0]["observed_value"] != _records[1]["observed_value"]


@pytest.mark.unit
def test_company_research_create_rejects_invalid_source_url() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/research",
            data={
                "csrf_token": CSRF_TOKEN,
                "record_type": "verified_fact",
                "body": "Funding round",
                "source_name": "Bad",
                "source_url": "javascript:alert(1)",
                "observed_value": "$10M",
                "observed_at": "2026-07-14T12:00:00Z",
                "confidence": "0.5",
                "review_at": "2026-08-14T12:00:00Z",
                "expires_at": "2026-09-14T12:00:00Z",
            },
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert len(_records) == 0


@pytest.mark.unit
def test_contact_research_page_escapes_xss_in_rendered_records() -> None:
    _records.append(
        {
            "record_type": "hypothesis",
            "body": '<script>alert("owned")</script>',
            "expires_at": None,
        }
    )
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/contacts/{CONTACT_ID}")
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "research-type-badge--hypothesis" in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.unit
def test_company_research_page_requires_existing_company() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            crm.get_company.return_value = None
            response = client.get(f"/admin/companies/{COMPANY_ID}")
    assert response.status_code == 404


@pytest.mark.unit
def test_contact_research_create_attaches_record() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/research",
            data={
                "csrf_token": CSRF_TOKEN,
                "record_type": "follow_up_note",
                "body": "Schedule check-in",
            },
        )
    assert response.status_code == 303
    assert len(_records) == 1
    assert _records[0]["record_type"] == "follow_up_note"


@pytest.mark.unit
def test_company_research_create_rejects_bad_csrf() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/research",
            data={
                "csrf_token": "wrong-token",
                "record_type": "hypothesis",
                "body": "Maybe expanding",
            },
        )
    assert response.status_code == 400


@pytest.mark.unit
def test_admin_dashboard_links_to_companies() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get("/admin")
    assert response.status_code == 200
    assert "/admin/companies" in response.text
