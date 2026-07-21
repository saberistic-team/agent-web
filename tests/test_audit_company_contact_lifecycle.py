"""Audit coverage for company and contact lifecycle mutations (#333)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service
from app.actor_context import ActorContext
from app.admin_pages import render_admin_audit_page
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactEmailConflictError, ContactUpdate
from app.crm_service import CrmService, CrmRepositories
from app.main import app
from app.repositories.postgres import PostgresCompanyRepository, PostgresContactRepository

client = TestClient(app, follow_redirects=False)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = ActorContext(actor="operator", correlation_id="corr-lifecycle-audit")
CSRF_TOKEN = "csrf-lifecycle-audit-token"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

SECRET_NOTES = "PRIVATE_NOTES_333"
SECRET_FUNDING = "Undisclosed Series Z round"
SECRET_EMAIL = "secret@example.com"
SECRET_PROFILE = "https://linkedin.com/in/ada?token=sk_live_333"


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


def _service_with(
    *,
    companies: MagicMock | None = None,
    contacts: MagicMock | None = None,
) -> tuple[CrmService, MagicMock]:
    service = CrmService(
        repos=CrmRepositories(
            companies=companies or MagicMock(),
            contacts=contacts or MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
        )
    )
    return service, MagicMock()


LIFECYCLE_ACTIONS = [
    ("create_company", audit_service.ACTION_COMPANY_CREATE, "record_company_create"),
    ("update_company", audit_service.ACTION_COMPANY_UPDATE, "record_company_update"),
    ("archive_company", audit_service.ACTION_COMPANY_ARCHIVE, "record_company_archive"),
    ("restore_company", audit_service.ACTION_COMPANY_RESTORE, "record_company_restore"),
    ("create_contact", audit_service.ACTION_CONTACT_CREATE, "record_contact_create"),
    ("update_contact", audit_service.ACTION_CONTACT_UPDATE, "record_contact_update"),
    ("archive_contact", audit_service.ACTION_CONTACT_ARCHIVE, "record_contact_archive"),
    ("restore_contact", audit_service.ACTION_CONTACT_RESTORE, "record_contact_restore"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method_name", "action", "record_helper"),
    LIFECYCLE_ACTIONS,
)
def test_each_lifecycle_action_writes_one_attributed_event(
    method_name: str,
    action: str,
    record_helper: str,
) -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    entity_id = uuid4()
    now = datetime.now(timezone.utc)
    company_repo.create.return_value = {
        "id": entity_id,
        "name": "Acme",
        "domain": "acme.dev",
    }
    company_repo.get_by_id.return_value = {
        "id": entity_id,
        "name": "Acme",
        "domain": "acme.dev",
        "archived_at": now,
    }
    company_repo.update.return_value = {
        "id": entity_id,
        "name": "Acme Updated",
        "domain": "acme.dev",
    }
    company_repo.archive.return_value = {
        "id": entity_id,
        "name": "Acme",
        "archived_at": now,
    }
    company_repo.restore.return_value = {
        "id": entity_id,
        "name": "Acme",
        "archived_at": None,
    }
    contact_repo.create.return_value = {
        "id": entity_id,
        "full_name": "Ada",
        "company_id": COMPANY_ID,
    }
    contact_repo.get_by_id.return_value = {
        "id": entity_id,
        "full_name": "Ada",
        "archived_at": now,
    }
    contact_repo.update.return_value = {
        "id": entity_id,
        "full_name": "Ada Updated",
    }
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_active_by_email.return_value = None
    contact_repo.archive.return_value = {
        "id": entity_id,
        "full_name": "Ada",
        "archived_at": now,
    }
    contact_repo.restore.return_value = {
        "id": entity_id,
        "full_name": "Ada",
        "archived_at": None,
    }
    service, conn = _service_with(companies=company_repo, contacts=contact_repo)

    with patch(f"app.crm_service.audit_service.{record_helper}") as audit:
        if method_name == "create_company":
            service.create_company(
                conn,
                company=CompanyCreate(name="Acme"),
                actor_context=ACTOR,
            )
        elif method_name == "update_company":
            service.update_company(
                conn,
                entity_id,
                company=CompanyUpdate(name="Acme Updated"),
                actor_context=ACTOR,
            )
        elif method_name == "archive_company":
            company_repo.get_by_id.return_value = {
                "id": entity_id,
                "name": "Acme",
                "archived_at": None,
            }
            service.archive_company(conn, entity_id, actor_context=ACTOR)
        elif method_name == "restore_company":
            service.restore_company(conn, entity_id, actor_context=ACTOR)
        elif method_name == "create_contact":
            service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada"),
                actor_context=ACTOR,
            )
        elif method_name == "update_contact":
            service.update_contact(
                conn,
                entity_id,
                contact=ContactUpdate(full_name="Ada Updated"),
                actor_context=ACTOR,
            )
        elif method_name == "archive_contact":
            contact_repo.get_by_id.return_value = {
                "id": entity_id,
                "full_name": "Ada",
                "archived_at": None,
            }
            service.archive_contact(conn, entity_id, actor_context=ACTOR)
        elif method_name == "restore_contact":
            contact_repo.get_active_by_email.return_value = None
            service.restore_contact(conn, entity_id, actor_context=ACTOR)

    audit.assert_called_once()
    audit_kwargs = audit.call_args.kwargs
    assert audit_kwargs["actor_context"] == ACTOR
    if record_helper == "record_contact_restore":
        assert audit_kwargs["contact_id"] == str(entity_id)
    else:
        assert audit_kwargs["entity_id"] == str(entity_id)


@pytest.mark.unit
def test_lifecycle_audit_json_excludes_sensitive_values() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_id = uuid4()
    contact_id = uuid4()
    company_repo.create.return_value = {
        "id": company_id,
        "name": "Acme",
        "notes": SECRET_NOTES,
        "funding_summary": SECRET_FUNDING,
    }
    contact_repo.create.return_value = {
        "id": contact_id,
        "full_name": "Ada",
        "email": SECRET_EMAIL,
        "profile_url": SECRET_PROFILE,
        "notes": SECRET_NOTES,
    }
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_active_by_email.return_value = None
    service, conn = _service_with(companies=company_repo, contacts=contact_repo)
    captured: list[dict[str, Any]] = []

    def capture_append(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt"}

    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.side_effect = capture_append
        get_repos.return_value.audit_events = audit_repo
        service.create_company(
            conn,
            company=CompanyCreate(name="Acme", notes=SECRET_NOTES, funding_summary=SECRET_FUNDING),
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
    for secret in (SECRET_NOTES, SECRET_FUNDING, SECRET_EMAIL, SECRET_PROFILE, "sk_live_333"):
        assert secret not in combined
    company_summary = captured[0]["summary_after"]
    assert company_summary["has_notes"] is True
    assert company_summary["has_funding_summary"] is True
    contact_summary = captured[1]["summary_after"]
    assert contact_summary["has_email"] is True
    assert contact_summary["has_profile_url"] is True
    assert contact_summary["has_notes"] is True


@pytest.mark.unit
def test_company_create_audit_failure_rolls_back_without_success_event() -> None:
    company_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    service, conn = _service_with(companies=company_repo)
    with patch(
        "app.crm_service.audit_service.record_company_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.create_company(
                conn,
                company=CompanyCreate(name="Acme"),
                actor_context=ACTOR,
            )
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_contact_create_repository_failure_writes_no_audit_event() -> None:
    contact_repo = MagicMock()
    contact_repo.create.side_effect = ValueError("invalid contact")
    service, conn = _service_with(contacts=contact_repo)
    with patch("app.crm_service.audit_service.record_contact_create") as audit:
        with pytest.raises(ValueError, match="invalid contact"):
            service.create_contact(
                conn,
                contact=ContactCreate(full_name="Ada"),
                actor_context=ACTOR,
            )
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_no_op_company_update_writes_no_audit_event() -> None:
    company_repo = MagicMock()
    existing = {"id": COMPANY_ID, "name": "Acme", "domain": "acme.dev"}
    company_repo.get_by_id.return_value = existing
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = existing
    service, conn = _service_with(companies=company_repo)
    with patch("app.crm_service.audit_service.record_company_update") as audit:
        service.update_company(
            conn,
            COMPANY_ID,
            company=CompanyUpdate(name="Acme", domain="acme.dev"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_no_op_contact_update_writes_no_audit_event() -> None:
    contact_repo = MagicMock()
    existing = {"id": CONTACT_ID, "full_name": "Ada", "title": "CTO"}
    contact_repo.get_by_id.return_value = existing
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.update.return_value = existing
    service, conn = _service_with(contacts=contact_repo)
    with patch("app.crm_service.audit_service.record_contact_update") as audit:
        service.update_contact(
            conn,
            CONTACT_ID,
            contact=ContactUpdate(full_name="Ada", title="CTO"),
            actor_context=ACTOR,
        )
    audit.assert_not_called()


@pytest.mark.unit
def test_archive_not_found_writes_no_success_event() -> None:
    company_repo = MagicMock()
    company_repo.get_by_id.return_value = None
    service, conn = _service_with(companies=company_repo)
    with patch("app.crm_service.audit_service.record_company_archive") as audit:
        assert service.archive_company(conn, COMPANY_ID, actor_context=ACTOR) is None
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_contact_email_conflict_on_create_writes_no_success_event() -> None:
    contact_repo = MagicMock()
    contact_repo.get_active_by_email.return_value = {"id": CONTACT_ID}
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    from psycopg.errors import UniqueViolation

    contact_repo.create.side_effect = UniqueViolation("duplicate email")
    service, conn = _service_with(contacts=contact_repo)
    with patch("app.crm_service.audit_service.record_contact_create") as audit:
        with patch(
            "app.crm_service._is_contact_email_unique_violation",
            return_value=True,
        ):
            with pytest.raises(ContactEmailConflictError):
                service.create_contact(
                    conn,
                    contact=ContactCreate(full_name="Ada", email=SECRET_EMAIL),
                    actor_context=ACTOR,
                )
    audit.assert_not_called()


@pytest.mark.unit
def test_route_passes_actor_context_and_correlation_id() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                conn = MagicMock()
                db_conn.return_value.__enter__.return_value = conn
                with patch("app.admin_routes._crm") as crm:
                    crm.create_company.return_value = {
                        "company": {"id": COMPANY_ID},
                        "duplicate_warnings": [],
                    }
                    response = client.post(
                        "/admin/companies",
                        data={"csrf_token": CSRF_TOKEN, "name": "Acme"},
                        headers={"X-Request-ID": "req-lifecycle-333"},
                    )
                    assert response.status_code == 303
                    actor = crm.create_company.call_args.kwargs["actor_context"]
                    assert actor.actor == ACTOR.actor
                    assert actor.correlation_id == "req-lifecycle-333"


@pytest.mark.unit
def test_anonymous_and_invalid_csrf_lifecycle_requests_do_not_mutate() -> None:
    unauthenticated = client.post(
        "/admin/companies",
        data={"csrf_token": CSRF_TOKEN, "name": "Nope"},
    )
    assert unauthenticated.status_code == 303
    assert "/admin/login" in unauthenticated.headers["location"]

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            bad_csrf = client.post(
                f"/admin/companies/{COMPANY_ID}/archive",
                data={"csrf_token": "wrong"},
            )
            assert bad_csrf.status_code == 400
            crm.archive_company.assert_not_called()


@pytest.mark.unit
def test_audit_ui_renders_lifecycle_events_with_bounded_summaries() -> None:
    attacker = '<img src=x onerror=alert(1)>'
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
                "name": attacker,
                "domain": "acme.dev",
                "category": "fintech",
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
    assert "Company create" in html_out
    assert "company.create" in html_out
    assert "Contact archive" in html_out
    assert "contact.archive" in html_out
    assert attacker not in html_out
    assert "&lt;img" in html_out
    assert "<script>" not in html_out


@pytest.mark.contract
def test_company_create_audit_failure_rolls_back_on_real_postgresql(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    service = CrmService()
    with patch(
        "app.crm_service.audit_service.record_company_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.create_company(
                migrated_conn,
                company=CompanyCreate(name="Rollback Co"),
                actor_context=ACTOR,
            )
    verifier = connect()
    count = verifier.execute("SELECT COUNT(*) AS n FROM companies WHERE name = %s", ("Rollback Co",)).fetchone()["n"]
    assert count == 0
    audit_count = verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
    assert audit_count == 0


@pytest.mark.contract
def test_concurrent_company_archive_records_one_success_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    repo = PostgresCompanyRepository()
    company = repo.create(migrated_conn, name="Race Co")
    company_id = company["id"]
    migrated_conn.commit()

    barrier = threading.Barrier(2, timeout=15)
    results: list[dict[str, Any] | None] = []
    errors: list[BaseException] = []

    def archive_once() -> None:
        conn = connect()
        try:
            barrier.wait()
            service = CrmService()
            archived = service.archive_company(conn, company_id, actor_context=ACTOR)
            conn.commit()
            results.append(archived)
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=archive_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sum(1 for result in results if result is not None) == 1
    assert sum(1 for result in results if result is None) == 1

    verifier = connect()
    archived_count = verifier.execute(
        "SELECT COUNT(*) AS n FROM companies WHERE id = %s AND archived_at IS NOT NULL",
        (company_id,),
    ).fetchone()["n"]
    assert archived_count == 1
    audit_count = verifier.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = %s AND entity_id = %s",
        (audit_service.ACTION_COMPANY_ARCHIVE, str(company_id)),
    ).fetchone()["n"]
    assert audit_count == 1
