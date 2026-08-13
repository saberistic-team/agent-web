"""Tests for the company contact-enrichment admin action."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.config import get_settings
from app.hunter_enrichment import HunterError
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

COMPANY_ID = uuid4()
_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, str], None, None]:
    raw_token = admin_auth.generate_session_token()
    token_hash = admin_auth.hash_session_token(raw_token)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": admin_auth.hash_csrf_token(
            admin_auth.derive_session_csrf_token(raw_token, get_settings())
        ),
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
        yield {SESSION_COOKIE_NAME: raw_token}


def _csrf(cookies: dict[str, str]) -> str:
    return admin_auth.derive_session_csrf_token(
        cookies[SESSION_COOKIE_NAME],
        get_settings(),
    )


def _company() -> dict[str, Any]:
    return {
        "id": str(COMPANY_ID),
        "name": "LedgerFlow",
        "domain": "ledgerflow.example",
        "status": "prospect",
    }


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_contacts_requires_auth() -> None:
    response = client.post(
        f"/admin/companies/{COMPANY_ID}/enrich-contacts",
        data={"csrf_token": "anonymous"},
    )
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_contacts_requires_configuration(
    authenticated_admin: dict[str, str],
) -> None:
    response = client.post(
        f"/admin/companies/{COMPANY_ID}/enrich-contacts",
        cookies=authenticated_admin,
        data={"csrf_token": _csrf(authenticated_admin)},
    )
    assert response.status_code == 303
    assert "error=Hunter.io%20is%20not%20configured" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_contacts_success_redirects_with_notice(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    outcome = {
        "company": _company(),
        "domain": "ledgerflow.example",
        "found": 3,
        "created": [{"id": str(uuid4())}],
        "skipped": ["info@ledgerflow.example"],
    }
    with patch("app.admin_routes._crm") as crm:
        crm.enrich_company_contacts.return_value = outcome
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/enrich-contacts",
            cookies=authenticated_admin,
            data={"csrf_token": _csrf(authenticated_admin)},
        )
    assert response.status_code == 303
    location = response.headers["location"]
    assert f"/admin/companies/{COMPANY_ID}" in location
    assert "3%20emails" in location
    assert "1%20contacts%20added" in location
    assert "1%20already%20in%20CRM" in location


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_contacts_hunter_error_redirects_with_message(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    with patch("app.admin_routes._crm") as crm:
        crm.enrich_company_contacts.side_effect = HunterError(
            "Hunter rate limit reached; try again later"
        )
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/enrich-contacts",
            cookies=authenticated_admin,
            data={"csrf_token": _csrf(authenticated_admin)},
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "rate%20limit" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_contacts_unknown_company_404(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    with patch("app.admin_routes._crm") as crm:
        crm.enrich_company_contacts.return_value = None
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/enrich-contacts",
            cookies=authenticated_admin,
            data={"csrf_token": _csrf(authenticated_admin)},
        )
    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.integration
def test_company_detail_shows_enrich_action_when_configured(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    with patch("app.admin_routes._crm") as crm:
        crm.get_company.return_value = _company()
        crm.list_contacts_for_company.return_value = []
        crm.list_research_for_company.return_value = []
        response = client.get(f"/admin/companies/{COMPANY_ID}", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Enrich contacts via Hunter.io" in response.text
    assert f'/admin/companies/{COMPANY_ID}/enrich-contacts' in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_company_detail_hides_enrich_action_when_unconfigured(
    authenticated_admin: dict[str, str],
) -> None:
    with patch("app.admin_routes._crm") as crm:
        crm.get_company.return_value = _company()
        crm.list_contacts_for_company.return_value = []
        crm.list_research_for_company.return_value = []
        response = client.get(f"/admin/companies/{COMPANY_ID}", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Enrich contacts via Hunter.io" not in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_company_detail_renders_notice(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    with patch("app.admin_routes._crm") as crm:
        crm.get_company.return_value = _company()
        crm.list_contacts_for_company.return_value = []
        crm.list_research_for_company.return_value = []
        response = client.get(
            f"/admin/companies/{COMPANY_ID}?notice=Hunter.io%20found%202%20emails",
            cookies=authenticated_admin,
        )
    assert response.status_code == 200
    assert "Hunter.io found 2 emails" in response.text
