"""Real-PostgreSQL audit contracts for company/contact lifecycle (#333)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest

from app import audit_service
from app.actor_context import ActorContext
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactEmailConflictError, ContactUpdate
from app.crm_service import CrmService
from app.repositories.postgres import PostgresCompanyRepository, PostgresContactRepository

ACTOR = ActorContext(actor="operator", correlation_id="corr-lifecycle-contract")


def _latest_audit(
    connect: Callable[..., psycopg.Connection],
    *,
    action: str,
) -> dict[str, Any]:
    conn = connect()
    row = conn.execute(
        """
        SELECT action, actor, correlation_id, entity_type, entity_id,
               summary_before, summary_after
        FROM audit_events
        WHERE action = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (action,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _assert_fresh(
    connect: Callable[..., psycopg.Connection],
    *,
    table: str,
    row_id: UUID,
    column: str,
    expected: Any,
) -> None:
    conn = connect()
    row = conn.execute(
        f"SELECT {column} AS value FROM {table} WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row is not None
    assert row["value"] == expected


@pytest.mark.contract
def test_company_create_audit_failure_rolls_back_mutation(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    service = CrmService()
    with patch(
        "app.crm_lifecycle_audit.audit_service.record_company_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.create_company(
                migrated_conn,
                company=CompanyCreate(name="Rollback Create Co"),
                actor_context=ACTOR,
            )
    verifier = connect()
    count = verifier.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    assert count == 0
    audit_count = verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
    assert audit_count == 0


@pytest.mark.contract
def test_contact_archive_audit_failure_rolls_back_mutation(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    contacts = PostgresContactRepository()
    service = CrmService()
    contact = contacts.create(migrated_conn, full_name="Archive Rollback")
    contact_id = contact["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_contact_archive",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.archive_contact(
                migrated_conn,
                contact_id,
                actor_context=ACTOR,
            )

    _assert_fresh(
        connect,
        table="contacts",
        row_id=contact_id,
        column="archived_at",
        expected=None,
    )
    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


@pytest.mark.contract
def test_company_restore_writes_transition_audit_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Restore Audit Co")
    company_id = company["id"]
    companies.archive(migrated_conn, company_id)
    migrated_conn.commit()

    service.restore_company(migrated_conn, company_id, actor_context=ACTOR)
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_COMPANY_RESTORE)
    assert event["actor"] == ACTOR.actor
    assert event["correlation_id"] == ACTOR.correlation_id
    assert event["entity_id"] == str(company_id)
    assert event["summary_before"]["archived_at"] is not None
    assert event["summary_after"]["archived_at"] is None
    assert event["summary_before"]["name"] == "Restore Audit Co"


@pytest.mark.contract
def test_no_op_company_update_writes_no_audit_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="No-op Co", notes="Same")
    company_id = company["id"]
    migrated_conn.commit()

    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="No-op Co", notes="Same"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


@pytest.mark.contract
def test_contact_email_conflict_on_create_writes_no_audit_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    service = CrmService()
    service.create_contact(
        migrated_conn,
        contact=ContactCreate(full_name="Owner", email="dup@example.com"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    with pytest.raises(ContactEmailConflictError):
        service.create_contact(
            migrated_conn,
            contact=ContactCreate(full_name="Duplicate", email="DUP@example.com"),
            actor_context=ACTOR,
        )

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 1


@pytest.mark.contract
def test_company_update_writes_audit_event_on_real_change(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Before Co", notes="Before")
    company_id = company["id"]
    migrated_conn.commit()

    service.update_company(
        migrated_conn,
        company_id,
        company=CompanyUpdate(name="Before Co", notes="After"),
        actor_context=ACTOR,
    )
    migrated_conn.commit()

    event = _latest_audit(connect, action=audit_service.ACTION_COMPANY_UPDATE)
    assert event["summary_before"]["notes"] == "Before"
    assert event["summary_after"]["notes"] == "After"


@pytest.mark.contract
def test_contact_create_writes_bounded_audit_event(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    service = CrmService()
    result = service.create_contact(
        migrated_conn,
        contact=ContactCreate(
            full_name="Audit Contact",
            email="secret@example.com",
            notes="Private notes",
        ),
        actor_context=ACTOR,
    )
    migrated_conn.commit()
    contact_id = result["contact"]["id"]

    event = _latest_audit(connect, action=audit_service.ACTION_CONTACT_CREATE)
    assert event["entity_id"] == str(contact_id)
    assert event["summary_after"]["full_name"] == "Audit Contact"
    assert "email" not in event["summary_after"]
