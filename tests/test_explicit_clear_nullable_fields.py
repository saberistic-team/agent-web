"""Explicit clear vs. replace vs. omit for nullable CRM fields (#230).

Covers the three-state patch contract at every boundary:

* repository — an explicit ``None`` writes SQL ``NULL`` while an omitted field
  (``UNSET``) is left out of the ``UPDATE`` entirely,
* service — form models pass only supplied fields, and clears persist through to
  the repository and audit summaries,
* form/route — blank nullable inputs become explicit clears.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.acquisition_pipeline import PipelineNextActionUpdate
from app.actor_context import ActorContext
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_routes import _company_form_payload, _contact_form_payload
from app.companies import CompanyUpdate
from app.contacts import ContactUpdate
from app.crm_service import CrmRepositories, CrmService
from app.main import app
from app.patch import UNSET
from app.repositories.postgres import (
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ACTOR = ActorContext(actor="operator", correlation_id="corr-1")


def _mock_conn(row: dict | None = None) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row or {"id": COMPANY_ID}
    return conn, cur


def _service_with(**repos: MagicMock) -> tuple[CrmService, MagicMock]:
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


# --------------------------------------------------------------------------- #
# Repository boundary — clear writes SQL NULL, omit is absent, replace binds it #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_company_repo_clear_writes_null_and_omit_is_absent() -> None:
    repo = PostgresCompanyRepository()

    # Clear: explicit None binds NULL for the column.
    conn, cur = _mock_conn({"id": COMPANY_ID, "notes": None})
    repo.update(conn, COMPANY_ID, notes=None, funding_summary=None)
    sql, values = cur.execute.call_args.args[0], cur.execute.call_args.args[1]
    assert "notes = %s" in sql and "funding_summary = %s" in sql
    assert values[0] is None and values[1] is None
    # Untouched columns are omitted so they keep their stored values.
    assert "category = %s" not in sql and "target_status = %s" not in sql

    # Replace: a value binds that value.
    conn2, cur2 = _mock_conn({"id": COMPANY_ID})
    repo.update(conn2, COMPANY_ID, category="fintech")
    sql2, values2 = cur2.execute.call_args.args[0], cur2.execute.call_args.args[1]
    assert "category = %s" in sql2 and values2[0] == "fintech"

    # Omit-only: nothing but the primary key read-through.
    conn3, cur3 = _mock_conn({"id": COMPANY_ID})
    repo.update(conn3, COMPANY_ID)
    assert "UPDATE companies" not in str(cur3.execute.call_args.args[0])


@pytest.mark.unit
def test_contact_repo_clear_email_and_disassociate_company() -> None:
    repo = PostgresContactRepository()
    conn, cur = _mock_conn({"id": CONTACT_ID, "email": None, "company_id": None})
    repo.update(conn, CONTACT_ID, email=None, company_id=None, title="Head of Eng")
    sql, values = cur.execute.call_args.args[0], cur.execute.call_args.args[1]
    assert "email = %s" in sql and "company_id = %s" in sql and "title = %s" in sql
    columns = [part.split(" = ")[0].strip() for part in sql.split("SET", 1)[1].split(",")]
    email_value = values[columns.index("email")]
    company_value = values[columns.index("company_id")]
    title_value = values[columns.index("title")]
    assert email_value is None  # clear -> SQL NULL
    assert company_value is None  # disassociate -> SQL NULL
    assert title_value == "Head of Eng"  # replace
    # Fields never supplied stay out of the statement.
    assert "profile_url = %s" not in sql and "notes = %s" not in sql


@pytest.mark.unit
def test_pipeline_repo_clear_next_action_writes_null() -> None:
    repo = PostgresPipelineRepository()
    conn, cur = _mock_conn({"id": COMPANY_ID})
    repo.update_pipeline_fields(
        conn,
        COMPANY_ID,
        next_action=None,
        next_action_due_at=None,
        pipeline_owner=None,
        expected_value_cents=None,
    )
    sql, values = cur.execute.call_args.args[0], cur.execute.call_args.args[1]
    for column in ("next_action", "next_action_due_at", "pipeline_owner", "expected_value_cents"):
        assert f"{column} = %s" in sql
    # All four supplied clears bind NULL (values list minus trailing updated_at/id).
    assert values[:4] == [None, None, None, None]


@pytest.mark.unit
def test_pipeline_repo_omit_keeps_value_and_flag_clears_reason() -> None:
    repo = PostgresPipelineRepository()
    conn, cur = _mock_conn({"id": COMPANY_ID})
    # Only next_action supplied; owner/value omitted; loss reason cleared by flag.
    repo.update_pipeline_fields(
        conn,
        COMPANY_ID,
        next_action="Send proposal",
        clear_loss_reason=True,
    )
    sql = cur.execute.call_args.args[0]
    assert "next_action = %s" in sql
    assert "pipeline_owner = %s" not in sql
    assert "expected_value_cents = %s" not in sql
    assert "pipeline_loss_reason = NULL" in sql


@pytest.mark.unit
def test_pipeline_repo_reason_value_and_clear_flag_do_not_double_assign() -> None:
    repo = PostgresPipelineRepository()
    conn, cur = _mock_conn({"id": COMPANY_ID})
    # A supplied reason value wins; the guard prevents a duplicate NULL assignment.
    repo.update_pipeline_fields(
        conn,
        COMPANY_ID,
        pipeline_loss_reason="Budget cut",
        clear_loss_reason=True,
    )
    sql = cur.execute.call_args.args[0]
    assert sql.count("pipeline_loss_reason") == 1
    assert "pipeline_loss_reason = %s" in sql
    assert "pipeline_loss_reason = NULL" not in sql


# --------------------------------------------------------------------------- #
# Service boundary                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_update_company_clears_supplied_fields_and_omits_others() -> None:
    company_repo = MagicMock()
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = {"id": COMPANY_ID, "name": "Acme"}
    service, conn = _service_with(companies=company_repo)

    # Form-shaped update: all fields present, notes/funding blanked (cleared).
    payload = _company_form_payload(
        name="Acme",
        website="",
        domain="",
        category="",
        stage="",
        headcount_estimate="",
        funding_summary="",
        target_status="",
        last_verified_at="",
        notes="",
    )
    service.update_company(conn, COMPANY_ID, company=CompanyUpdate(**payload))
    kwargs = company_repo.update.call_args.kwargs
    assert kwargs["notes"] is None  # cleared
    assert kwargs["funding_summary"] is None  # cleared
    assert kwargs["name"] == "Acme"  # preserved non-null value

    # Partial programmatic update omits everything but name.
    company_repo.update.reset_mock()
    service.update_company(conn, COMPANY_ID, company=CompanyUpdate(name="Acme Renamed"))
    partial_kwargs = company_repo.update.call_args.kwargs
    assert partial_kwargs == {"name": "Acme Renamed"}
    assert "notes" not in partial_kwargs  # omitted -> UNSET default -> unchanged


@pytest.mark.unit
def test_update_contact_clears_email_and_disassociates_company() -> None:
    contact_repo = MagicMock()
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_by_email.return_value = None
    contact_repo.update.return_value = {"id": CONTACT_ID, "full_name": "Ada"}
    service, conn = _service_with(contacts=contact_repo)

    payload = _contact_form_payload(
        full_name="Ada",
        title="",
        profile_url="",
        email="",
        email_permission="",
        company_id="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=[],
    )
    service.update_contact(conn, CONTACT_ID, contact=ContactUpdate(**payload))
    kwargs = contact_repo.update.call_args.kwargs
    assert kwargs["email"] is None  # cleared
    assert kwargs["company_id"] is None  # disassociated
    assert kwargs["title"] is None and kwargs["notes"] is None
    assert kwargs["full_name"] == "Ada"  # preserved non-null value


@pytest.mark.unit
def test_update_company_clear_is_audited_as_change() -> None:
    company_repo = MagicMock()
    before = {
        "id": COMPANY_ID,
        "name": "Acme",
        "notes": "Keep me",
        "funding_summary": "Clear me",
    }
    after = {**before, "funding_summary": None}
    company_repo.get_by_id.return_value = before
    company_repo.find_by_domain.return_value = []
    company_repo.update.return_value = after
    service, conn = _service_with(companies=company_repo)

    with patch("app.crm_service.audit_service.record_company_update") as audit:
        service.update_company(
            conn,
            COMPANY_ID,
            company=CompanyUpdate(name="Acme", funding_summary=None),
            actor_context=ACTOR,
        )

    audit.assert_called_once()
    audit_kwargs = audit.call_args.kwargs
    assert audit_kwargs["summary_before"]["notes"] == "Keep me"
    assert audit_kwargs["summary_after"]["notes"] == "Keep me"
    assert audit_kwargs["summary_before"]["funding_summary"] == "Clear me"
    assert audit_kwargs["summary_after"]["funding_summary"] is None
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_update_contact_clear_is_audited_as_change() -> None:
    contact_repo = MagicMock()
    before = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "email": "ada@example.com",
        "title": "Keep me",
        "notes": "Clear me",
    }
    after = {**before, "notes": None}
    contact_repo.get_by_id.return_value = before
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.update.return_value = after
    service, conn = _service_with(contacts=contact_repo)

    with patch("app.crm_service.audit_service.record_contact_update") as audit:
        service.update_contact(
            conn,
            CONTACT_ID,
            contact=ContactUpdate(full_name="Ada", notes=None),
            actor_context=ACTOR,
        )

    audit.assert_called_once()
    audit_kwargs = audit.call_args.kwargs
    assert audit_kwargs["summary_before"]["title"] == "Keep me"
    assert audit_kwargs["summary_after"]["title"] == "Keep me"
    assert audit_kwargs["summary_before"]["notes"] == "Clear me"
    assert audit_kwargs["summary_after"]["notes"] is None
    assert "email" not in audit_kwargs["summary_before"]
    assert "email" not in audit_kwargs["summary_after"]
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_update_pipeline_next_action_clear_is_audited_as_change() -> None:
    pipeline_repo = MagicMock()
    before = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Call founder",
        "next_action_due_at": None,
        "pipeline_owner": "operator",
        "expected_value_cents": 50_000,
    }
    after = {**before, "next_action": None, "pipeline_owner": None, "expected_value_cents": None}
    pipeline_repo.get_company_pipeline.return_value = before
    pipeline_repo.update_pipeline_fields.return_value = after
    service, conn = _service_with(pipeline=pipeline_repo)

    with patch("app.crm_service.audit_service.record_pipeline_update") as audit:
        service.update_pipeline_next_action(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            update=PipelineNextActionUpdate(
                next_action="",  # blank -> explicit clear
                next_action_due_at=None,
                pipeline_owner="",
                expected_value_cents=None,
            ),
        )

    # The clear is forwarded to the repository as an explicit None.
    forwarded = pipeline_repo.update_pipeline_fields.call_args.kwargs
    assert forwarded["next_action"] is None
    assert forwarded["pipeline_owner"] is None

    # Audit distinguishes clear (before value, after null) from unchanged.
    audit.assert_called_once()
    audit_kwargs = audit.call_args.kwargs
    assert audit_kwargs["summary_before"]["next_action"] == "Call founder"
    assert audit_kwargs["summary_after"]["next_action"] is None
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_clearing_archived_contact_email_unblocks_restore() -> None:
    """Clearing an archived contact's email removes the restore email conflict."""
    contact_repo = MagicMock()
    contact_repo.find_by_profile_url.return_value = []
    contact_repo.find_by_name_company.return_value = []
    contact_repo.get_by_email.return_value = None
    # Step 1: clear the archived contact's conflicting email.
    contact_repo.update.return_value = {"id": CONTACT_ID, "full_name": "Ada", "email": None}
    service, conn = _service_with(contacts=contact_repo)

    payload = _contact_form_payload(
        full_name="Ada",
        title="",
        profile_url="",
        email="",  # clear the conflicting email
        email_permission="",
        company_id="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=[],
    )
    service.update_contact(conn, CONTACT_ID, contact=ContactUpdate(**payload))
    assert contact_repo.update.call_args.kwargs["email"] is None

    # Step 2: with the email cleared, restore no longer detects a conflict.
    contact_repo.get_by_id.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "email": None,
        "archived_at": "2026-01-01",
    }
    contact_repo.restore.return_value = {"id": CONTACT_ID, "archived_at": None}
    with patch("app.crm_service.audit_service.record_contact_restore"):
        result = service.restore_contact(conn, CONTACT_ID, actor_context=ACTOR)
    assert result.outcome == "success"
    contact_repo.get_active_by_email.assert_not_called()


@pytest.mark.unit
def test_update_pipeline_next_action_omitted_field_left_unchanged() -> None:
    pipeline_repo = MagicMock()
    pipeline_repo.get_company_pipeline.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Keep me",
    }
    pipeline_repo.update_pipeline_fields.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Keep me",
        "pipeline_owner": "Owner",
    }
    service, conn = _service_with(pipeline=pipeline_repo)

    with patch("app.crm_service.audit_service.record_pipeline_update"):
        service.update_pipeline_next_action(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            update=PipelineNextActionUpdate(pipeline_owner="Owner"),
        )
    forwarded = pipeline_repo.update_pipeline_fields.call_args.kwargs
    assert forwarded == {"pipeline_owner": "Owner"}
    assert "next_action" not in forwarded  # omitted -> stored value preserved


# --------------------------------------------------------------------------- #
# Form parsing boundary                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_company_form_payload_maps_blanks_to_none() -> None:
    payload = _company_form_payload(
        name="Acme",
        website="",
        domain="",
        category="",
        stage="",
        headcount_estimate="",
        funding_summary="",
        target_status="",
        last_verified_at="",
        notes="",
    )
    for field in ("website", "domain", "funding_summary", "target_status", "last_verified_at", "notes", "headcount_estimate"):
        assert payload[field] is None
    # Cleared fields are still present in the validated patch (explicit clear).
    dumped = CompanyUpdate(**payload).model_dump(exclude_unset=True)
    assert dumped["notes"] is None and dumped["funding_summary"] is None


@pytest.mark.unit
def test_contact_form_payload_maps_blanks_to_none() -> None:
    payload = _contact_form_payload(
        full_name="Ada",
        title="",
        profile_url="",
        email="",
        email_permission="",
        company_id="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=[],
    )
    for field in ("title", "profile_url", "email", "email_permission", "company_id", "last_interaction_at", "relationship_strength", "notes"):
        assert payload[field] is None
    dumped = ContactUpdate(**payload).model_dump(exclude_unset=True)
    assert dumped["email"] is None and dumped["company_id"] is None


# --------------------------------------------------------------------------- #
# Route boundary — posting blanks clears through the service                  #
# --------------------------------------------------------------------------- #

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
_session_store: dict[str, dict[str, Any]] = {}
client = TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> dict[str, Any]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
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
        patch("app.admin_pipeline_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/contacts/new", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1)}


@pytest.mark.unit
def test_company_edit_route_clears_blank_fields(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.update_company.return_value = {"company": {"id": COMPANY_ID}, "duplicate_warnings": []}
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/edit",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "name": "Acme",
                "website": "",
                "domain": "",
                "category": "",
                "stage": "",
                "headcount_estimate": "",
                "funding_summary": "",
                "target_status": "",
                "last_verified_at": "",
                "notes": "",
            },
        )
    assert response.status_code == 303
    company: CompanyUpdate = crm.update_company.call_args.kwargs["company"]
    dumped = company.model_dump(exclude_unset=True)
    assert dumped["notes"] is None and dumped["funding_summary"] is None
    assert dumped["domain"] is None  # blanked domain is a clear, not re-derived


@pytest.mark.unit
def test_contact_edit_route_clears_blank_fields(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_contact.return_value = {"id": CONTACT_ID, "full_name": "Ada"}
    crm.list_companies.return_value = []
    crm.update_contact.return_value = {"contact": {"id": CONTACT_ID}, "duplicate_warnings": []}
    with patch("app.admin_routes._crm", crm):
        response = client.post(
            f"/admin/contacts/{CONTACT_ID}/edit",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "full_name": "Ada",
                "title": "",
                "profile_url": "",
                "email": "",
                "email_permission": "",
                "company_id": "",
                "last_interaction_at": "",
                "relationship_strength": "",
                "notes": "",
            },
        )
    assert response.status_code == 303
    contact: ContactUpdate = crm.update_contact.call_args.kwargs["contact"]
    dumped = contact.model_dump(exclude_unset=True)
    assert dumped["email"] is None and dumped["company_id"] is None


@pytest.mark.unit
def test_pipeline_next_action_route_clears_blanks(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "next_action": "",
                "next_action_due_at": "",
                "pipeline_owner": "",
                "expected_value_cents": "",
            },
        )
    assert response.status_code == 303
    update: PipelineNextActionUpdate = crm.update_pipeline_next_action.call_args.kwargs["update"]
    dumped = update.model_dump(exclude_unset=True)
    assert dumped["next_action"] is None
    assert dumped["pipeline_owner"] is None
    assert dumped["expected_value_cents"] is None
