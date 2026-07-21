"""Real-PostgreSQL audit contracts for company and contact lifecycle (#333)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from app import audit_service
from app.actor_context import ActorContext
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactUpdate
from app.crm_service import CrmService
from app.repositories.postgres import PostgresCompanyRepository, PostgresContactRepository

ACTOR_A = ActorContext(actor="operator-a", correlation_id="corr-lifecycle-a")
ACTOR_B = ActorContext(actor="operator-b", correlation_id="corr-lifecycle-b")

SECRET_NOTES = "CONFIDENTIAL_NOTES_333"
SECRET_EMAIL = "ceo@secret.example"
SECRET_PROFILE = "https://linkedin.com/in/ceo?session=sk_live_secret_333"


def _audit_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM audit_events ORDER BY created_at ASC, id ASC")
        return [dict(row) for row in cur.fetchall()]


def _fresh_company(
    connect: Callable[..., psycopg.Connection],
    company_id: Any,
) -> dict[str, Any]:
    verifier = connect()
    row = verifier.execute(
        "SELECT id, name, archived_at, notes FROM companies WHERE id = %s",
        (company_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.contract
def test_company_create_persists_bounded_audit_in_postgres(
    migrated_conn: psycopg.Connection,
) -> None:
    service = CrmService()
    created = service.create_company(
        migrated_conn,
        company=CompanyCreate(
            name="Audit Create Co",
            domain="audit-create.example",
            notes=SECRET_NOTES,
            funding_summary="Seed round",
        ),
        actor_context=ACTOR_A,
    )
    company_id = created["company"]["id"]

    rows = _audit_rows(migrated_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == audit_service.ACTION_COMPANY_CREATE
    assert row["entity_id"] == str(company_id)
    assert row["actor"] == ACTOR_A.actor
    assert row["correlation_id"] == ACTOR_A.correlation_id
    blob = json.dumps(row["summary_after"])
    assert SECRET_NOTES not in blob
    assert row["summary_after"]["has_notes"] is True


@pytest.mark.contract
def test_company_create_audit_failure_rolls_back_mutation(
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
                actor_context=ACTOR_A,
            )

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"] == 0
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


@pytest.mark.contract
def test_company_archive_and_restore_record_state_transition(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Archive Co", domain="archive.example")
    company_id = company["id"]
    migrated_conn.commit()

    archived = service.archive_company(
        migrated_conn, company_id, actor_context=ACTOR_A
    )
    assert archived is not None
    assert archived["archived_at"] is not None

    restored = service.restore_company(
        migrated_conn, company_id, actor_context=ACTOR_B
    )
    assert restored is not None
    assert restored["archived_at"] is None

    rows = _audit_rows(migrated_conn)
    assert [row["action"] for row in rows] == [
        audit_service.ACTION_COMPANY_ARCHIVE,
        audit_service.ACTION_COMPANY_RESTORE,
    ]
    assert rows[0]["summary_before"]["archived_at"] is None
    assert rows[0]["summary_after"]["archived_at"] is not None
    assert rows[1]["summary_before"]["archived_at"] is not None
    assert rows[1]["summary_after"]["archived_at"] is None


@pytest.mark.contract
def test_contact_create_excludes_email_and_profile_url_from_audit(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Employer")
    contact = service.create_contact(
        migrated_conn,
        contact=ContactCreate(
            full_name="Audit Contact",
            email=SECRET_EMAIL,
            profile_url=SECRET_PROFILE,
            notes=SECRET_NOTES,
            company_id=company["id"],
        ),
        actor_context=ACTOR_A,
    )
    migrated_conn.commit()

    row = _audit_rows(migrated_conn)[0]
    blob = json.dumps(row)
    assert SECRET_EMAIL not in blob
    assert SECRET_PROFILE not in blob
    assert SECRET_NOTES not in blob
    assert row["summary_after"]["has_profile_url"] is True
    assert row["summary_after"]["has_notes"] is True
    assert row["entity_id"] == str(contact["contact"]["id"])


@pytest.mark.contract
def test_no_op_update_writes_no_audit_event(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="No-op Co", category="fintech")
    company_id = company["id"]
    migrated_conn.commit()

    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="No-op Co"),
        actor_context=ACTOR_A,
    )
    migrated_conn.commit()

    assert _audit_rows(migrated_conn) == []


@pytest.mark.contract
def test_concurrent_company_archive_preserves_single_winner_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Concurrent Archive Co")
    company_id = company["id"]
    migrated_conn.commit()

    barrier = threading.Barrier(2)
    results: list[bool] = []

    def attempt(actor: ActorContext) -> None:
        worker = connect()
        barrier.wait()
        archived = service.archive_company(worker, company_id, actor_context=actor)
        worker.commit()
        results.append(archived is not None)

    threads = [
        threading.Thread(target=attempt, args=(ACTOR_A,)),
        threading.Thread(target=attempt, args=(ACTOR_B,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1
    fresh = _fresh_company(connect, company_id)
    assert fresh["archived_at"] is not None
    archive_rows = [
        row
        for row in _audit_rows(migrated_conn)
        if row["action"] == audit_service.ACTION_COMPANY_ARCHIVE
    ]
    assert len(archive_rows) == 1


@pytest.mark.contract
def test_contact_update_audit_failure_rolls_back_mutation(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    contacts = PostgresContactRepository()
    service = CrmService()
    contact = contacts.create(
        migrated_conn, full_name="Rollback Contact", title="Before"
    )
    contact_id = contact["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_contact_update",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.update_contact(
                migrated_conn,
                contact_id,
                contact=ContactUpdate(full_name="Rollback Contact", title="After"),
                actor_context=ACTOR_A,
            )

    verifier = connect()
    row = verifier.execute(
        "SELECT title FROM contacts WHERE id = %s",
        (contact_id,),
    ).fetchone()
    assert row is not None
    assert row["title"] == "Before"
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0
