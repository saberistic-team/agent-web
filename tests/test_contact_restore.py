"""Tests for contact restoration with active email conflict handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import admin_auth, audit_service
from app.actor_context import ActorContext
from app.contacts import ContactRestoreResult, ContactSafeSummary
from app.crm_service import CrmService, CrmRepositories
from app.main import app

pytestmark = pytest.mark.unit

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
CSRF_TOKEN = "csrf-test-token"

ARCHIVED_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTIVE_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
COMPANY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

_archived = {
    "id": ARCHIVED_ID,
    "full_name": "Ada Lovelace",
    "title": "Former CTO",
    "email": "ada@example.com",
    "company_id": COMPANY_ID,
    "archived_at": datetime.now(timezone.utc),
}
_active = {
    "id": ACTIVE_ID,
    "full_name": "Ada Lovelace (current)",
    "title": "CTO",
    "email": "ada@example.com",
    "company_id": COMPANY_ID,
    "company_name": "Acme Corp",
    "archived_at": None,
}


def _actor() -> ActorContext:
    return ActorContext(actor=TEST_USERNAME, correlation_id="corr-restore-test")


def _fake_session() -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=1,
        admin_username=TEST_USERNAME,
        token_hash="session-hash",
        csrf_token_hash=admin_auth.hash_csrf_token(CSRF_TOKEN),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _service_with_contact_repo(contact_repo: MagicMock) -> tuple[CrmService, MagicMock]:
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
        )
    )
    return service, MagicMock()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def test_restore_contact_succeeds_without_email_conflict() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {**_archived, "email": "unique@example.com"}
    contact_repo.get_active_by_email.return_value = None
    contact_repo.restore.return_value = {
        **_archived,
        "email": "unique@example.com",
        "archived_at": None,
    }
    service, conn = _service_with_contact_repo(contact_repo)

    with patch("app.crm_service.audit_service.record_contact_restore") as audit:
        result = service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor())

    assert result.outcome == "success"
    assert result.contact is not None
    assert result.contact["archived_at"] is None
    contact_repo.restore.assert_called_once_with(conn, ARCHIVED_ID)
    audit.assert_called_once()
    conn.commit.assert_called_once()


def test_restore_contact_detects_case_insensitive_email_conflict() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {**_archived, "email": "ADA@example.com"}
    contact_repo.get_active_by_email.return_value = _active
    service, conn = _service_with_contact_repo(contact_repo)

    with patch("app.crm_service.audit_service.record_contact_restore") as audit:
        result = service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor())

    assert result.outcome == "conflict"
    assert result.conflicting_contact == contact_safe_summary_from(_active)
    contact_repo.restore.assert_not_called()
    audit.assert_not_called()
    conn.commit.assert_not_called()


def test_restore_contact_without_email_restores_normally() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {**_archived, "email": None}
    contact_repo.restore.return_value = {**_archived, "email": None, "archived_at": None}
    service, conn = _service_with_contact_repo(contact_repo)

    with patch("app.crm_service.audit_service.record_contact_restore") as audit:
        result = service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor())

    assert result.outcome == "success"
    contact_repo.get_active_by_email.assert_not_called()
    audit.assert_called_once()


def test_restore_contact_handles_concurrent_unique_violation() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = _archived
    contact_repo.get_active_by_email.side_effect = [None, _active]
    contact_repo.restore.side_effect = UniqueViolation("duplicate key")
    service, conn = _service_with_contact_repo(contact_repo)

    with (
        patch("app.crm_service.audit_service.record_contact_restore") as audit,
        patch(
            "app.crm_service._is_contact_email_unique_violation",
            return_value=True,
        ),
    ):
        result = service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor())

    assert result.outcome == "conflict"
    assert result.conflicting_contact is not None
    audit.assert_not_called()
    conn.rollback.assert_called_once()


def test_restore_contact_not_found_for_missing_or_active() -> None:
    contact_repo = MagicMock()
    service, conn = _service_with_contact_repo(contact_repo)

    contact_repo.get_by_id.return_value = None
    assert service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor()).outcome == "not_found"

    contact_repo.get_by_id.return_value = {**_archived, "archived_at": None}
    assert service.restore_contact(conn, ARCHIVED_ID, actor_context=_actor()).outcome == "not_found"


def test_get_contact_restore_conflict_returns_conflict_state() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = _archived
    contact_repo.get_active_by_email.return_value = _active
    service, conn = _service_with_contact_repo(contact_repo)

    result = service.get_contact_restore_conflict(conn, ARCHIVED_ID)
    assert result is not None
    assert result.outcome == "conflict"
    assert result.archived_contact == _archived


def contact_safe_summary_from(contact: dict[str, Any]) -> ContactSafeSummary:
    from app.contacts import contact_safe_summary

    return contact_safe_summary(contact, company_name=contact.get("company_name"))


def test_restore_conflict_page_renders_safe_fields_and_links() -> None:
    from app.admin_contacts import render_contact_restore_conflict_page

    html = render_contact_restore_conflict_page(
        csrf_token=CSRF_TOKEN,
        admin_username=TEST_USERNAME,
        archived_contact=_archived,
        conflicting_contact=contact_safe_summary_from(_active),
        company_name="Acme Corp",
    )
    assert "Restore blocked" in html
    assert "Ada Lovelace" in html
    assert "ada@example.com" in html
    assert "Acme Corp" in html
    assert f'/admin/contacts/{ARCHIVED_ID}/edit' in html
    assert f'/admin/contacts/{ACTIVE_ID}/edit' in html
    assert "never merged automatically" in html
    assert "notes" not in html.lower()


@pytest.fixture
def _mock_crm_restore() -> Generator[MagicMock, None, None]:
    crm = MagicMock()
    crm.list_companies.return_value = [{"id": COMPANY_ID, "name": "Acme Corp"}]
    crm.get_company.return_value = {"id": COMPANY_ID, "name": "Acme Corp"}
    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN),
        patch(
            "app.admin_auth.verify_session_csrf_request",
            side_effect=lambda _request, submitted, _settings: submitted == CSRF_TOKEN,
        ),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        yield crm


def test_restore_route_redirects_on_success(_mock_crm_restore: MagicMock) -> None:
    _mock_crm_restore.restore_contact.return_value = ContactRestoreResult(
        outcome="success",
        contact={**_archived, "archived_at": None},
    )
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{ARCHIVED_ID}/restore",
            data={"csrf_token": CSRF_TOKEN},
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/contacts/{ARCHIVED_ID}/edit"
    assert "actor_context" in _mock_crm_restore.restore_contact.call_args.kwargs


def test_restore_route_redirects_to_conflict_page(_mock_crm_restore: MagicMock) -> None:
    _mock_crm_restore.restore_contact.return_value = ContactRestoreResult(
        outcome="conflict",
        archived_contact=_archived,
        conflicting_contact=contact_safe_summary_from(_active),
    )
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.post(
            f"/admin/contacts/{ARCHIVED_ID}/restore",
            data={"csrf_token": CSRF_TOKEN},
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/contacts/{ARCHIVED_ID}/restore-conflict"


def test_restore_conflict_get_renders_conflict_state(_mock_crm_restore: MagicMock) -> None:
    _mock_crm_restore.get_contact_restore_conflict.return_value = ContactRestoreResult(
        outcome="conflict",
        archived_contact=_archived,
        conflicting_contact=contact_safe_summary_from(_active),
    )
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(f"/admin/contacts/{ARCHIVED_ID}/restore-conflict")
    assert response.status_code == 200
    assert "Restore blocked" in response.text
    assert "Ada Lovelace (current)" in response.text


def test_restore_requires_authentication_and_csrf(_mock_crm_restore: MagicMock) -> None:
    unauth = client.post(
        f"/admin/contacts/{ARCHIVED_ID}/restore",
        data={"csrf_token": CSRF_TOKEN},
    )
    assert unauth.status_code == 303
    assert unauth.headers["location"].startswith("/admin/login")
    _mock_crm_restore.restore_contact.assert_not_called()

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        bad_csrf = client.post(
            f"/admin/contacts/{ARCHIVED_ID}/restore",
            data={"csrf_token": "wrong"},
        )
    assert bad_csrf.status_code == 400
    _mock_crm_restore.restore_contact.assert_not_called()


def test_restore_preserves_related_records_via_non_destructive_update() -> None:
    from app.repositories.postgres import PostgresContactRepository

    repo = PostgresContactRepository()
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {
        "id": ARCHIVED_ID,
        "full_name": "Ada",
        "email": "ada@example.com",
        "company_id": COMPANY_ID,
        "archived_at": None,
    }
    repo.restore(conn, ARCHIVED_ID)
    sql = str(cursor.execute.call_args.args[0])
    assert "archived_at = NULL" in sql
    assert "full_name" not in sql
    assert "company_id" not in sql
    assert "DELETE" not in sql


def test_record_contact_restore_redacts_email() -> None:
    conn = MagicMock()
    actor = _actor()
    with patch("app.audit_service.get_repositories") as repos:
        repos.return_value.audit_events.append.return_value = {"id": "evt-1"}
        audit_service.record_contact_restore(
            conn,
            actor_context=actor,
            contact_id=str(ARCHIVED_ID),
            summary_before={"archived_at": "x", "email": "secret@example.com"},
            summary_after={"archived_at": None, "email": "secret@example.com"},
        )
    payload = repos.return_value.audit_events.append.call_args.kwargs
    assert payload["action"] == audit_service.ACTION_CONTACT_RESTORE
    assert payload["summary_before"]["email"] == audit_service.REDACTED_VALUE


def test_preview_contact_restore_conflict_stable_with_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import preview_contact_restore_conflict

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T12:00:00+00:00")
    a = preview_contact_restore_conflict()
    b = preview_contact_restore_conflict()
    assert a == b
    assert a["archived_contact"]["email"]
    assert a["conflicting_contact"]["full_name"]


def test_preview_restore_conflict_route_renders_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    from app.admin_preview import PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        response = client.get(
            f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID}/restore-conflict"
        )
    assert response.status_code == 200
    assert "Restore blocked" in response.text
    assert "never merged automatically" in response.text
