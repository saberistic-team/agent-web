"""Audit coverage for company/contact lifecycle mutations (#333)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from app import admin_auth, audit_service
from app.actor_context import ActorContext
from app.admin_pages import render_admin_audit_page
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactEmailConflictError, ContactRestoreResult, ContactUpdate
from app.crm_service import CrmRepositories, CrmService
from app.main import app

client = TestClient(app, follow_redirects=False)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = ActorContext(actor="operator", correlation_id="corr-lifecycle-audit")
CSRF_TOKEN = "csrf-lifecycle-audit-token"
TEST_USERNAME = ACTOR.actor
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

SECRET_EMAIL = "ceo@secret.example"
SECRET_NOTES = "CONFIDENTIAL_NOTES_333"
SECRET_PROFILE = "https://linkedin.com/in/ada?token=sk_live_secret_333"
SECRET_FUNDING = "Undisclosed Series Z round led by secret@example.com"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.admin_username = TEST_USERNAME
    session.csrf_token_hash = "hash"
    return session


def _company_service(**repo_overrides: MagicMock) -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    repos = {
        "companies": MagicMock(),
        "contacts": MagicMock(),
        "source_records": MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": MagicMock(),
        "icp_scoring": MagicMock(),
        "qualification": MagicMock(),
    }
    repos.update(repo_overrides)
    return CrmService(repos=CrmRepositories(**repos)), MagicMock(), repos


def _contact_service(**repo_overrides: MagicMock) -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    return _company_service(**repo_overrides)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method_name", "action", "call"),
    [
        (
            "create_company",
            audit_service.ACTION_COMPANY_CREATE,
            lambda service, conn: service.create_company(
                conn,
                company=CompanyCreate(name="Acme Labs", domain="acme.example"),
                actor_context=ACTOR,
            ),
        ),
        (
            "update_company",
            audit_service.ACTION_COMPANY_UPDATE,
            lambda service, conn: service.update_company(
                conn,
                COMPANY_ID,
                company=CompanyUpdate(name="Acme Labs Renamed"),
                actor_context=ACTOR,
            ),
        ),
        (
            "archive_company",
            audit_service.ACTION_COMPANY_ARCHIVE,
            lambda service, conn: service.archive_company(
                conn, COMPANY_ID, actor_context=ACTOR
            ),
        ),
        (
            "restore_company",
            audit_service.ACTION_COMPANY_RESTORE,
            lambda service, conn: service.restore_company(
                conn, COMPANY_ID, actor_context=ACTOR
            ),
        ),
        (
            "create_contact",
            audit_service.ACTION_CONTACT_CREATE,
            lambda service, conn: service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada Lovelace", email=SECRET_EMAIL),
                actor_context=ACTOR,
            ),
        ),
        (
            "update_contact",
            audit_service.ACTION_CONTACT_UPDATE,
            lambda service, conn: service.update_contact(
                conn,
                CONTACT_ID,
                contact=ContactUpdate(full_name="Ada Lovelace Updated"),
                actor_context=ACTOR,
            ),
        ),
        (
            "archive_contact",
            audit_service.ACTION_CONTACT_ARCHIVE,
            lambda service, conn: service.archive_contact(
                conn, CONTACT_ID, actor_context=ACTOR
            ),
        ),
        (
            "restore_contact",
            audit_service.ACTION_CONTACT_RESTORE,
            lambda service, conn: service.restore_contact(
                conn, CONTACT_ID, actor_context=ACTOR
            ),
        ),
    ],
)
def test_lifecycle_action_writes_one_attributed_event(
    method_name: str,
    action: str,
    call: Any,
) -> None:
    del method_name
    company_repo = MagicMock()
    contact_repo = MagicMock()
    archived_at = datetime.now(timezone.utc)
    company_repo.create.return_value = {
        "id": COMPANY_ID,
        "name": "Acme Labs",
        "domain": "acme.example",
        "notes": SECRET_NOTES,
        "funding_summary": SECRET_FUNDING,
    }
    if action == audit_service.ACTION_COMPANY_RESTORE:
        company_repo.get_by_id.return_value = {
            "id": COMPANY_ID,
            "name": "Acme Labs",
            "archived_at": archived_at,
            "notes": SECRET_NOTES,
        }
    else:
        company_repo.get_by_id.return_value = {
            "id": COMPANY_ID,
            "name": "Acme Labs",
            "archived_at": None,
            "notes": SECRET_NOTES,
        }
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = {
        "id": COMPANY_ID,
        "name": "Acme Labs Renamed",
        "notes": SECRET_NOTES,
    }
    company_repo.archive.return_value = {
        "id": COMPANY_ID,
        "name": "Acme Labs",
        "archived_at": archived_at,
    }
    company_repo.restore.return_value = {
        "id": COMPANY_ID,
        "name": "Acme Labs",
        "archived_at": None,
    }
    contact_repo.create.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "email": SECRET_EMAIL,
        "profile_url": SECRET_PROFILE,
        "notes": SECRET_NOTES,
    }
    if action == audit_service.ACTION_CONTACT_RESTORE:
        contact_repo.get_by_id.return_value = {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "email": SECRET_EMAIL,
            "profile_url": SECRET_PROFILE,
            "archived_at": archived_at,
            "notes": SECRET_NOTES,
        }
    else:
        contact_repo.get_by_id.return_value = {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "email": SECRET_EMAIL,
            "profile_url": SECRET_PROFILE,
            "archived_at": None,
            "notes": SECRET_NOTES,
        }
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_active_by_email.return_value = None
    contact_repo.update.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace Updated",
        "profile_url": SECRET_PROFILE,
        "notes": SECRET_NOTES,
    }
    contact_repo.archive.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "archived_at": archived_at,
    }
    contact_repo.restore.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "archived_at": None,
    }
    service, conn, _ = _company_service(companies=company_repo, contacts=contact_repo)

    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.return_value = {"id": "evt-1"}
        get_repos.return_value.audit_events = audit_repo
        call(service, conn)

    audit_repo.append.assert_called_once()
    payload = audit_repo.append.call_args.kwargs
    assert payload["action"] == action
    assert payload["actor"] == ACTOR.actor
    assert payload["correlation_id"] == ACTOR.correlation_id
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_lifecycle_audit_json_excludes_sensitive_values() -> None:
    company_repo = MagicMock()
    company_repo.create.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "domain": "acme.example",
        "notes": SECRET_NOTES,
        "funding_summary": SECRET_FUNDING,
        "website": "https://acme.example?token=secret",
    }
    company_repo.find_by_domain.return_value = []
    contact_repo = MagicMock()
    contact_repo.create.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "email": SECRET_EMAIL,
        "profile_url": SECRET_PROFILE,
        "notes": SECRET_NOTES,
    }
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_active_by_email.return_value = None
    service, conn, _ = _company_service(companies=company_repo, contacts=contact_repo)
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
            company=CompanyCreate(name="Acme", domain="acme.example", notes=SECRET_NOTES),
            actor_context=ACTOR,
        )
        service.create_contact(
            conn,
            contact=ContactCreate(
                full_name="Ada",
                email=SECRET_EMAIL,
                profile_url=SECRET_PROFILE,
                notes=SECRET_NOTES,
            ),
            actor_context=ACTOR,
        )

    combined = json.dumps(captured)
    assert SECRET_EMAIL not in combined
    contact_summary = captured[1]["summary_after"]
    assert "email" not in contact_summary


@pytest.mark.unit
def test_company_create_audit_failure_rolls_back_without_success_event() -> None:
    company_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Rollback Co"}
    company_repo.find_by_domain.return_value = []
    service, conn, _ = _company_service(companies=company_repo)
    with patch(
        "app.crm_lifecycle_audit.audit_service.record_company_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.create_company(
                conn,
                company=CompanyCreate(name="Rollback Co"),
                actor_context=ACTOR,
            )
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_contact_create_repository_failure_writes_no_audit_event() -> None:
    contact_repo = MagicMock()
    contact_repo.create.side_effect = ValueError("invalid contact")
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    service, conn, _ = _contact_service(contacts=contact_repo)
    with patch("app.crm_lifecycle_audit.audit_service.record_contact_create") as audit:
        with pytest.raises(ValueError, match="invalid contact"):
            service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada"),
                actor_context=ACTOR,
            )
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_no_op_company_update_writes_no_event() -> None:
    company_repo = MagicMock()
    unchanged = {
        "id": COMPANY_ID,
        "name": "Acme",
        "notes": "Same",
        "domain": "acme.example",
    }
    company_repo.get_by_id.return_value = unchanged
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = unchanged
    service, conn, _ = _company_service(companies=company_repo)
    with patch("app.crm_lifecycle_audit.audit_service.record_company_update") as audit:
        service.update_company(
            conn,
            COMPANY_ID,
            company=CompanyUpdate(name="Acme"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_no_op_contact_update_writes_no_event() -> None:
    contact_repo = MagicMock()
    unchanged = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "title": "CTO",
        "notes": "Same",
    }
    contact_repo.get_by_id.return_value = unchanged
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.update.return_value = unchanged
    service, conn, _ = _contact_service(contacts=contact_repo)
    with patch("app.crm_lifecycle_audit.audit_service.record_contact_update") as audit:
        service.update_contact(
            conn,
            CONTACT_ID,
            contact=ContactUpdate(full_name="Ada"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_archive_not_found_writes_no_success_event() -> None:
    company_repo = MagicMock()
    company_repo.get_by_id.return_value = None
    service, conn, _ = _company_service(companies=company_repo)
    with patch("app.crm_service.audit_service.record_company_archive") as audit:
        assert service.archive_company(conn, COMPANY_ID, actor_context=ACTOR) is None
    audit.assert_not_called()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_restore_not_found_writes_no_success_event() -> None:
    company_repo = MagicMock()
    company_repo.get_by_id.return_value = None
    service, conn, _ = _company_service(companies=company_repo)
    with patch("app.crm_service.audit_service.record_company_restore") as audit:
        assert service.restore_company(conn, COMPANY_ID, actor_context=ACTOR) is None
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_contact_create_email_conflict_writes_no_success_event() -> None:
    contact_repo = MagicMock()
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_active_by_email.return_value = None

    def _raise_unique(*_args: Any, **_kwargs: Any) -> None:
        raise UniqueViolation("duplicate active email")

    contact_repo.create.side_effect = _raise_unique
    service, conn, _ = _contact_service(contacts=contact_repo)
    with (
        patch("app.crm_service._is_contact_email_unique_violation", return_value=True),
        patch("app.crm_lifecycle_audit.audit_service.record_contact_create") as audit,
    ):
        with pytest.raises(ContactEmailConflictError):
            service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada", email=SECRET_EMAIL),
                actor_context=ACTOR,
            )
    audit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_concurrent_company_archive_only_winner_audits() -> None:
    company_repo = MagicMock()
    archived_at = datetime.now(timezone.utc)
    active = {"id": COMPANY_ID, "name": "Acme", "archived_at": None}
    archived = {"id": COMPANY_ID, "name": "Acme", "archived_at": archived_at}
    company_repo.get_by_id.side_effect = [active, active, {**active, "archived_at": archived_at}]
    company_repo.archive.side_effect = [archived, None]
    service, conn, _ = _company_service(companies=company_repo)
    with patch("app.crm_service.audit_service.record_company_archive") as audit:
        winner = service.archive_company(conn, COMPANY_ID, actor_context=ACTOR)
        loser = service.archive_company(conn, COMPANY_ID, actor_context=ACTOR)
    assert winner is not None
    assert loser is None
    audit.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "crm_method", "payload"),
    [
        (
            "/admin/companies",
            "create_company",
            {"csrf_token": CSRF_TOKEN, "name": "Acme"},
        ),
        (
            f"/admin/companies/{COMPANY_ID}/edit",
            "update_company",
            {"csrf_token": CSRF_TOKEN, "name": "Acme"},
        ),
        (
            f"/admin/companies/{COMPANY_ID}/archive",
            "archive_company",
            {"csrf_token": CSRF_TOKEN},
        ),
        (
            f"/admin/companies/{COMPANY_ID}/restore",
            "restore_company",
            {"csrf_token": CSRF_TOKEN},
        ),
        (
            "/admin/contacts",
            "create_contact",
            {"csrf_token": CSRF_TOKEN, "full_name": "Ada"},
        ),
        (
            f"/admin/contacts/{CONTACT_ID}/edit",
            "update_contact",
            {"csrf_token": CSRF_TOKEN, "full_name": "Ada"},
        ),
        (
            f"/admin/contacts/{CONTACT_ID}/archive",
            "archive_contact",
            {"csrf_token": CSRF_TOKEN},
        ),
        (
            f"/admin/contacts/{CONTACT_ID}/restore",
            "restore_contact",
            {"csrf_token": CSRF_TOKEN},
        ),
    ],
)
def test_lifecycle_routes_pass_actor_context(route: str, crm_method: str, payload: dict[str, str]) -> None:
    crm = MagicMock()
    crm.create_company.return_value = {"company": {"id": COMPANY_ID}, "duplicate_warnings": []}
    crm.update_company.return_value = {"company": {"id": COMPANY_ID}, "duplicate_warnings": []}
    crm.archive_company.return_value = {"id": COMPANY_ID}
    crm.restore_company.return_value = {"id": COMPANY_ID}
    crm.create_contact.return_value = {"contact": {"id": CONTACT_ID}, "duplicate_warnings": []}
    crm.update_contact.return_value = {"contact": {"id": CONTACT_ID}, "duplicate_warnings": []}
    crm.archive_contact.return_value = {"id": CONTACT_ID}
    crm.restore_contact.return_value = ContactRestoreResult(
        outcome="success",
        contact={"id": CONTACT_ID},
    )
    crm.get_contact.return_value = {"id": CONTACT_ID, "full_name": "Ada"}
    crm.list_companies.return_value = []
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
        response = client.post(route, data=payload, headers={"X-Request-ID": ACTOR.correlation_id})
    assert response.status_code in {303, 404}
    kwargs = getattr(crm, crm_method).call_args.kwargs
    assert kwargs["actor_context"].actor == ACTOR.actor
    assert kwargs["actor_context"].correlation_id == ACTOR.correlation_id


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "crm_method"),
    [
        (f"/admin/companies/{COMPANY_ID}/archive", "archive_company"),
        (f"/admin/contacts/{CONTACT_ID}/archive", "archive_contact"),
    ],
)
def test_anonymous_and_invalid_csrf_lifecycle_requests_do_not_mutate(
    route: str,
    crm_method: str,
) -> None:
    unauthenticated = client.post(route, data={"csrf_token": CSRF_TOKEN})
    assert unauthenticated.status_code == 303
    assert "/admin/login" in unauthenticated.headers["location"]

    crm = MagicMock()
    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.require_admin_session", return_value=_fake_session()),
    ):
        bad_csrf = client.post(route, data={"csrf_token": "wrong"})
    assert bad_csrf.status_code == 400
    getattr(crm, crm_method).assert_not_called()


@pytest.mark.unit
def test_audit_ui_renders_lifecycle_events_with_bounded_summaries() -> None:
    archived_at = "2026-07-14T12:00:00+00:00"
    events = [
        {
            "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_COMPANY_CREATE,
            "entity_type": "company",
            "entity_id": str(COMPANY_ID),
            "correlation_id": ACTOR.correlation_id,
            "summary_before": None,
            "summary_after": {"name": "Acme", "domain": "acme.example"},
        },
        {
            "created_at": datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_CONTACT_ARCHIVE,
            "entity_type": "contact",
            "entity_id": str(CONTACT_ID),
            "correlation_id": ACTOR.correlation_id,
            "summary_before": {"full_name": "Ada", "archived_at": None},
            "summary_after": {"full_name": "Ada", "archived_at": archived_at},
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
    assert "name=Acme" in html_out
    assert "full_name=Ada" in html_out
    assert "<script>" not in html_out


@pytest.mark.unit
def test_audit_ui_escapes_attacker_controlled_lifecycle_labels() -> None:
    html_out = render_admin_audit_page(
        admin_username=TEST_USERNAME,
        events=[
            {
                "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
                "actor": "<script>alert(1)</script>",
                "action": audit_service.ACTION_COMPANY_ARCHIVE,
                "entity_type": "company",
                "entity_id": "1",
                "correlation_id": "corr<script>",
                "summary_before": {"name": "<img onerror=alert(1)>", "archived_at": None},
                "summary_after": {
                    "name": "<img onerror=alert(1)>",
                    "archived_at": "2026-07-14T12:00:00+00:00",
                },
            }
        ],
        page=1,
        per_page=50,
        total=1,
    )
    assert "<script>alert(1)</script>" not in html_out
    assert "<img onerror=alert(1)>" not in html_out
    assert "&lt;script&gt;" in html_out


@pytest.mark.unit
def test_audit_summaries_equal_treats_redacted_fields_as_equal() -> None:
    before = {"email": "a@example.com", "name": "Acme"}
    after = {"email": "b@example.com", "name": "Acme"}
    assert audit_service.audit_summaries_equal(before, after) is True
