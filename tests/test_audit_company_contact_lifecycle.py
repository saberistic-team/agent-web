"""Audit coverage for company and contact lifecycle mutations (#333)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import admin_auth, audit_service
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactEmailConflictError, ContactUpdate
from app.actor_context import ActorContext
from app.admin_pages import render_admin_audit_page
from app.crm_service import CrmRepositories, CrmService
from app.main import app

client = TestClient(app, follow_redirects=False)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = ActorContext(actor="operator", correlation_id="corr-lifecycle-audit")
CSRF_TOKEN = "csrf-lifecycle-audit-token"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

SECRET_NOTES = "CONFIDENTIAL_NOTES_333"
SECRET_EMAIL = "ceo@secret.example"
SECRET_PROFILE = "https://linkedin.com/in/ceo?session=sk_live_secret_333"
SECRET_FUNDING = "Undisclosed Series Z round led by stealth fund"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", ACTOR.actor)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.admin_username = ACTOR.actor
    session.csrf_token_hash = "hash"
    return session


def _service(**repos: MagicMock) -> tuple[CrmService, MagicMock]:
    bundle = {
        "companies": MagicMock(),
        "contacts": MagicMock(),
        "source_records": MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": MagicMock(),
    }
    bundle.update(repos)
    return CrmService(repos=CrmRepositories(**bundle)), MagicMock()


def _company_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": COMPANY_ID,
        "name": "Acme Corp",
        "domain": "acme.example",
        "category": "fintech",
        "notes": SECRET_NOTES,
        "funding_summary": SECRET_FUNDING,
        "archived_at": None,
    }
    row.update(overrides)
    return row


def _contact_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "CTO",
        "email": SECRET_EMAIL,
        "profile_url": SECRET_PROFILE,
        "notes": SECRET_NOTES,
        "company_id": COMPANY_ID,
        "buying_roles": ["founder"],
        "archived_at": None,
    }
    row.update(overrides)
    return row


LIFECYCLE_ACTIONS = (
    (audit_service.ACTION_COMPANY_CREATE, "create_company", "companies", "record_company_create"),
    (audit_service.ACTION_COMPANY_UPDATE, "update_company", "companies", "record_company_update"),
    (audit_service.ACTION_COMPANY_ARCHIVE, "archive_company", "companies", "record_company_archive"),
    (audit_service.ACTION_COMPANY_RESTORE, "restore_company", "companies", "record_company_restore"),
    (audit_service.ACTION_CONTACT_CREATE, "create_contact", "contacts", "record_contact_create"),
    (audit_service.ACTION_CONTACT_UPDATE, "update_contact", "contacts", "record_contact_update"),
    (audit_service.ACTION_CONTACT_ARCHIVE, "archive_contact", "contacts", "record_contact_archive"),
    (audit_service.ACTION_CONTACT_RESTORE, "restore_contact", "contacts", "record_contact_restore"),
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "action,method_name,repo_name,audit_helper",
    LIFECYCLE_ACTIONS,
)
def test_each_lifecycle_action_writes_one_attributed_event(
    action: str,
    method_name: str,
    repo_name: str,
    audit_helper: str,
) -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.find_by_domain.return_value = []
    company_repo.create.return_value = _company_row()
    company_repo.get_by_id.return_value = _company_row()
    company_repo.update.return_value = _company_row(name="Acme Updated")
    company_repo.archive.return_value = _company_row(
        archived_at=datetime.now(timezone.utc),
    )
    company_repo.restore.return_value = _company_row(archived_at=None)

    contact_repo.find_by_profile_url.return_value = []
    contact_repo.get_active_by_email.return_value = None
    contact_repo.find_by_name_company.return_value = []
    contact_repo.create.return_value = _contact_row()
    contact_repo.get_by_id.side_effect = None
    contact_repo.get_by_id.return_value = _contact_row()
    contact_repo.update.return_value = _contact_row(title="VP Engineering")
    contact_repo.archive.return_value = _contact_row(
        archived_at=datetime.now(timezone.utc),
    )
    contact_repo.restore.return_value = _contact_row(archived_at=None)

    service, conn = _service(companies=company_repo, contacts=contact_repo)
    captured: list[dict[str, Any]] = []

    def capture_append(_conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt"}

    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.side_effect = capture_append
        get_repos.return_value.audit_events = audit_repo

        if method_name == "create_company":
            service.create_company(
                conn,
                company=CompanyCreate(name="Acme Corp", domain="acme.example"),
                actor_context=ACTOR,
            )
        elif method_name == "update_company":
            service.update_company(
                conn,
                COMPANY_ID,
                company=CompanyUpdate(name="Acme Updated"),
                actor_context=ACTOR,
            )
        elif method_name == "archive_company":
            service.archive_company(conn, COMPANY_ID, actor_context=ACTOR)
        elif method_name == "restore_company":
            company_repo.get_by_id.return_value = _company_row(
                archived_at=datetime.now(timezone.utc),
            )
            service.restore_company(conn, COMPANY_ID, actor_context=ACTOR)
        elif method_name == "create_contact":
            service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada Lovelace"),
                actor_context=ACTOR,
            )
        elif method_name == "update_contact":
            contact_repo.get_by_id.return_value = _contact_row()
            service.update_contact(
                conn,
                CONTACT_ID,
                contact=ContactUpdate(full_name="Ada Lovelace", title="VP Engineering"),
                actor_context=ACTOR,
            )
        elif method_name == "archive_contact":
            contact_repo.get_by_id.return_value = _contact_row()
            service.archive_contact(conn, CONTACT_ID, actor_context=ACTOR)
        elif method_name == "restore_contact":
            contact_repo.get_by_id.return_value = _contact_row(
                archived_at=datetime.now(timezone.utc),
            )
            service.restore_contact(conn, CONTACT_ID, actor_context=ACTOR)

    assert len(captured) == 1
    payload = captured[0]
    assert payload["action"] == action
    assert payload["actor"] == ACTOR.actor
    assert payload["correlation_id"] == ACTOR.correlation_id
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_lifecycle_audit_json_excludes_sensitive_free_form_values() -> None:
    company_repo = MagicMock()
    company_repo.find_by_domain.return_value = []
    company_repo.create.return_value = _company_row()
    service, conn = _service(companies=company_repo)
    captured: list[dict[str, Any]] = []

    def capture_append(_conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt"}

    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.side_effect = capture_append
        get_repos.return_value.audit_events = audit_repo
        service.create_company(
            conn,
            company=CompanyCreate(
                name="Acme Corp",
                domain="acme.example",
                notes=SECRET_NOTES,
                funding_summary=SECRET_FUNDING,
            ),
            actor_context=ACTOR,
        )

    blob = json.dumps(captured[0])
    for secret in (SECRET_NOTES, SECRET_FUNDING, SECRET_EMAIL, SECRET_PROFILE, "sk_live_secret_333"):
        assert secret not in blob
    summary = captured[0]["summary_after"]
    assert summary["has_notes"] is True
    assert summary["has_funding_summary"] is True
    assert "notes" not in summary
    assert "funding_summary" not in summary


@pytest.mark.unit
def test_no_op_company_update_writes_no_audit_event() -> None:
    company_repo = MagicMock()
    row = _company_row()
    company_repo.get_by_id.return_value = row
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = row
    service, conn = _service(companies=company_repo)

    with patch("app.crm_service.audit_service.record_company_update") as audit:
        service.update_company(
            conn,
            COMPANY_ID,
            company=CompanyUpdate(name="Acme Corp"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_no_op_contact_update_writes_no_audit_event() -> None:
    contact_repo = MagicMock()
    row = _contact_row()
    contact_repo.get_by_id.return_value = row
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.update.return_value = row
    service, conn = _service(contacts=contact_repo)

    with patch("app.crm_service.audit_service.record_contact_update") as audit:
        service.update_contact(
            conn,
            CONTACT_ID,
            contact=ContactUpdate(full_name="Ada Lovelace"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()


@pytest.mark.unit
def test_archive_not_found_writes_no_success_event() -> None:
    company_repo = MagicMock()
    company_repo.get_by_id.return_value = None
    service, conn = _service(companies=company_repo)

    with patch("app.crm_service.audit_service.record_company_archive") as audit:
        assert service.archive_company(conn, COMPANY_ID, actor_context=ACTOR) is None
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_contact_email_conflict_writes_no_success_event() -> None:
    contact_repo = MagicMock()
    contact_repo.get_active_by_email.return_value = None
    contact_repo.create.side_effect = UniqueViolation("duplicate key")
    service, conn = _service(contacts=contact_repo)

    with (
        patch("app.crm_service.audit_service.record_contact_create") as audit,
        patch("app.crm_service._is_contact_email_unique_violation", return_value=True),
        pytest.raises(ContactEmailConflictError),
    ):
        service.create_contact(
            conn,
            contact=ContactCreate(full_name="Ada", email=SECRET_EMAIL),
            actor_context=ACTOR,
        )
    audit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_audit_failure_rolls_back_company_create() -> None:
    company_repo = MagicMock()
    company_repo.find_by_domain.return_value = []
    company_repo.create.return_value = _company_row()
    service, conn = _service(companies=company_repo)

    with patch(
        "app.crm_service.audit_service.record_company_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.create_company(
                conn,
                company=CompanyCreate(name="Acme Corp"),
                actor_context=ACTOR,
            )
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_repository_failure_writes_no_success_event() -> None:
    company_repo = MagicMock()
    company_repo.find_by_domain.return_value = []
    company_repo.create.side_effect = ValueError("invalid company")
    service, conn = _service(companies=company_repo)

    with patch("app.crm_service.audit_service.record_company_create") as audit:
        with pytest.raises(ValueError, match="invalid company"):
            service.create_company(
                conn,
                company=CompanyCreate(name="Acme Corp"),
                actor_context=ACTOR,
            )
    audit.assert_not_called()


@pytest.mark.unit
def test_concurrent_archive_only_winner_writes_audit_event() -> None:
    company_repo = MagicMock()
    active = _company_row()
    company_repo.get_by_id.return_value = active
    company_repo.archive.side_effect = [None, _company_row(archived_at=datetime.now(timezone.utc))]
    service, conn = _service(companies=company_repo)

    with patch("app.crm_service.audit_service.record_company_archive") as audit:
        assert service.archive_company(conn, COMPANY_ID, actor_context=ACTOR) is None
        assert service.archive_company(conn, COMPANY_ID, actor_context=ACTOR) is not None
    audit.assert_called_once()


@pytest.mark.parametrize(
    "path,method_name",
    [
        ("/admin/companies", "create_company"),
        (f"/admin/companies/{COMPANY_ID}/archive", "archive_company"),
        (f"/admin/companies/{COMPANY_ID}/restore", "restore_company"),
        ("/admin/contacts", "create_contact"),
        (f"/admin/contacts/{CONTACT_ID}/archive", "archive_contact"),
    ],
)
@pytest.mark.unit
def test_lifecycle_routes_pass_actor_context(path: str, method_name: str) -> None:
    crm = MagicMock()
    crm.create_company.return_value = {"company": {"id": COMPANY_ID}, "duplicate_warnings": []}
    crm.create_contact.return_value = {"contact": {"id": CONTACT_ID}, "duplicate_warnings": []}
    crm.archive_company.return_value = {"id": COMPANY_ID}
    crm.restore_company.return_value = {"id": COMPANY_ID}
    crm.archive_contact.return_value = {"id": CONTACT_ID}

    form_data: dict[str, str] = {"csrf_token": CSRF_TOKEN}
    if method_name == "create_company":
        form_data["name"] = "Acme Corp"
    elif method_name == "create_contact":
        form_data["full_name"] = "Ada Lovelace"

    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN),
        patch(
            "app.admin_auth.verify_session_csrf_request",
            side_effect=lambda _request, submitted, _settings: submitted == CSRF_TOKEN,
        ),
        patch("app.admin_routes.require_admin_session", return_value=_fake_session()),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.post(path, data=form_data, headers={"X-Request-ID": ACTOR.correlation_id})

    assert response.status_code == 303
    kwargs = getattr(crm, method_name).call_args.kwargs
    assert kwargs["actor_context"].actor == ACTOR.actor
    assert kwargs["actor_context"].correlation_id == ACTOR.correlation_id


@pytest.mark.unit
def test_anonymous_and_invalid_csrf_lifecycle_requests_do_not_mutate() -> None:
    cases = [
        ("/admin/companies", {"csrf_token": CSRF_TOKEN, "name": "Acme"}),
        (f"/admin/companies/{COMPANY_ID}/archive", {"csrf_token": CSRF_TOKEN}),
        ("/admin/contacts", {"csrf_token": CSRF_TOKEN, "full_name": "Ada"}),
    ]
    for path, data in cases:
        unauthenticated = client.post(path, data=data)
        assert unauthenticated.status_code == 303
        assert "/admin/login" in unauthenticated.headers["location"]

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            bad = client.post(
                "/admin/companies",
                data={"csrf_token": "wrong", "name": "Acme"},
            )
            assert bad.status_code == 400
            crm.create_company.assert_not_called()


@pytest.mark.unit
def test_audit_ui_renders_lifecycle_events_with_bounded_summaries() -> None:
    events = [
        {
            "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_COMPANY_CREATE,
            "entity_type": "company",
            "entity_id": str(COMPANY_ID),
            "correlation_id": ACTOR.correlation_id,
            "summary_before": None,
            "summary_after": {
                "name": '"><script>alert(1)</script>',
                "domain": "acme.example",
            },
        },
        {
            "created_at": datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_CONTACT_ARCHIVE,
            "entity_type": "contact",
            "entity_id": str(CONTACT_ID),
            "correlation_id": ACTOR.correlation_id,
            "summary_before": {"full_name": "Ada", "archived_at": None},
            "summary_after": {
                "full_name": "Ada",
                "archived_at": "2026-07-14T13:00:00+00:00",
            },
        },
    ]
    html_out = render_admin_audit_page(
        admin_username=ACTOR.actor,
        events=events,
        page=1,
        per_page=50,
        total=2,
    )
    assert "Company created" in html_out
    assert "company.create" in html_out
    assert "Contact archived" in html_out
    assert "contact.archive" in html_out
    assert "name=" in html_out
    assert "<script>" not in html_out
