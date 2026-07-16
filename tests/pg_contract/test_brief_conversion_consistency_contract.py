"""Brief-conversion company/contact consistency contracts (#274).

Real PostgreSQL coverage for the company-association rule, transaction-time
contact revalidation, active-email uniqueness races, rollback, and idempotency.
Uses separate backend connections and barriers where concurrency matters.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest

from app.actor_context import ActorContext
from app.brief_conversion import BriefConversionValidationError
from app.crm_service import CrmService

ACTOR = ActorContext(actor="operator", correlation_id="corr-brief-consistency")
CRM_TABLES = (
    "companies",
    "contacts",
    "source_records",
    "activities",
    "pipeline_stage_history",
    "audit_events",
)


def _insert_company(
    conn: psycopg.Connection,
    db,
    *,
    name: str,
    domain: str,
    website: str | None = None,
) -> dict[str, Any]:
    row = db.fetch_dict(
        conn,
        """
        INSERT INTO companies (name, website, domain, pipeline_stage)
        VALUES (%s, %s, %s, 'researching')
        RETURNING *
        """,
        (name, website or f"https://{domain}", domain),
    )
    assert row is not None
    conn.commit()
    return row


def _insert_contact(
    conn: psycopg.Connection,
    db,
    *,
    full_name: str,
    email: str,
    company_id: UUID | None = None,
) -> dict[str, Any]:
    row = db.fetch_dict(
        conn,
        """
        INSERT INTO contacts (full_name, email, company_id, buying_roles)
        VALUES (%s, %s, %s, '{}')
        RETURNING *
        """,
        (full_name, email, company_id),
    )
    assert row is not None
    conn.commit()
    return row


def _counts(db, conn: psycopg.Connection) -> dict[str, int]:
    return {table: db.count(conn, table) for table in CRM_TABLES}


def test_new_company_rejects_contact_assigned_elsewhere(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    other = _insert_company(
        migrated_conn, db, name="Other Co", domain="other.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(other["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    before = _counts(db, connect())

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=UUID(str(contact["id"])),
        )

    after = _counts(db, connect())
    assert after == before


def test_existing_company_rejects_mismatched_contact(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    target = _insert_company(
        migrated_conn, db, name="Acme", domain="acme.example"
    )
    other = _insert_company(
        migrated_conn, db, name="Other Co", domain="other.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(other["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    before = _counts(db, connect())

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=UUID(str(target["id"])),
            selected_contact_id=UUID(str(contact["id"])),
        )

    after = _counts(db, connect())
    assert after == before


def test_new_company_attaches_unassigned_contact(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=None,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    service = CrmService()

    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="existing",
        selected_contact_id=UUID(str(contact["id"])),
    )

    assert result["idempotent"] is False
    company_id = UUID(str(result["company"]["id"]))
    verifier = connect()
    row = db.fetch_dict(
        verifier,
        "SELECT company_id FROM contacts WHERE id = %s",
        (UUID(str(contact["id"])),),
    )
    assert row is not None
    assert UUID(str(row["company_id"])) == company_id
    assert UUID(str(result["contact"]["company_id"])) == company_id
    assert db.count(verifier, "source_records") == 1


def test_existing_company_attaches_unassigned_contact(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company = _insert_company(
        migrated_conn, db, name="Acme", domain="acme.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=None,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    service = CrmService()

    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=UUID(str(company["id"])),
        selected_contact_id=UUID(str(contact["id"])),
    )

    company_id = UUID(str(company["id"]))
    verifier = connect()
    row = db.fetch_dict(
        verifier,
        "SELECT company_id FROM contacts WHERE id = %s",
        (UUID(str(contact["id"])),),
    )
    assert row is not None
    assert UUID(str(row["company_id"])) == company_id
    assert UUID(str(result["source_record"]["company_id"])) == company_id
    assert UUID(str(result["source_record"]["contact_id"])) == UUID(str(contact["id"]))


def test_matching_existing_company_and_contact_convert_successfully(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company = _insert_company(
        migrated_conn, db, name="Acme", domain="acme.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(company["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    service = CrmService()

    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=UUID(str(company["id"])),
        selected_contact_id=UUID(str(contact["id"])),
    )

    company_id = UUID(str(company["id"]))
    contact_id = UUID(str(contact["id"]))
    verifier = connect()
    source = db.fetch_dict(
        verifier,
        """
        SELECT company_id, contact_id
        FROM source_records
        WHERE source_type = 'project_brief' AND external_id = %s
        """,
        (str(brief["id"]),),
    )
    assert source is not None
    assert UUID(str(source["company_id"])) == company_id
    assert UUID(str(source["contact_id"])) == contact_id
    assert UUID(str(result["contact"]["company_id"])) == company_id


def test_conversion_rejects_contact_archived_during_transaction(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company = _insert_company(
        migrated_conn, db, name="Acme", domain="acme.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(company["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    contact_id = UUID(str(contact["id"]))
    company_id = UUID(str(company["id"]))

    entered = threading.Event()
    release = threading.Event()
    service_on_main = CrmService()
    real_resolve = service_on_main._resolve_existing_contact_for_conversion

    def gated_resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return real_resolve(*args, **kwargs)

    errors: list[BaseException] = []

    def archive_contact() -> None:
        assert entered.wait(5)
        archiver = connect()
        archiver.execute(
            "UPDATE contacts SET archived_at = NOW() WHERE id = %s",
            (contact_id,),
        )
        archiver.commit()
        release.set()

    def run_conversion() -> None:
        conn = connect()
        service = CrmService()
        try:
            with patch.object(
                service,
                "_resolve_existing_contact_for_conversion",
                side_effect=gated_resolve,
            ):
                service.convert_project_brief(
                    conn,
                    brief=brief,
                    actor_context=ACTOR,
                    price_cents=20_000,
                    company_choice="existing",
                    contact_choice="existing",
                    selected_company_id=company_id,
                    selected_contact_id=contact_id,
                )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_conversion)
    archiver = threading.Thread(target=archive_contact)
    worker.start()
    archiver.start()
    worker.join(timeout=15)
    archiver.join(timeout=15)

    assert len(errors) == 1
    assert isinstance(errors[0], BriefConversionValidationError)
    assert "no longer active" in str(errors[0]).lower()

    verifier = connect()
    assert db.count(verifier, "source_records") == 0


def test_conversion_rejects_contact_reassigned_during_transaction(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    target = _insert_company(
        migrated_conn, db, name="Acme", domain="acme.example"
    )
    other = _insert_company(
        migrated_conn, db, name="Other Co", domain="other.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(target["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    contact_id = UUID(str(contact["id"]))
    target_id = UUID(str(target["id"]))
    other_id = UUID(str(other["id"]))

    entered = threading.Event()
    release = threading.Event()
    service_on_main = CrmService()
    real_resolve = service_on_main._resolve_existing_contact_for_conversion

    def gated_resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return real_resolve(*args, **kwargs)

    errors: list[BaseException] = []

    def reassign_contact() -> None:
        assert entered.wait(5)
        mover = connect()
        mover.execute(
            "UPDATE contacts SET company_id = %s WHERE id = %s",
            (other_id, contact_id),
        )
        mover.commit()
        release.set()

    def run_conversion() -> None:
        conn = connect()
        service = CrmService()
        try:
            with patch.object(
                service,
                "_resolve_existing_contact_for_conversion",
                side_effect=gated_resolve,
            ):
                service.convert_project_brief(
                    conn,
                    brief=brief,
                    actor_context=ACTOR,
                    price_cents=20_000,
                    company_choice="existing",
                    contact_choice="existing",
                    selected_company_id=target_id,
                    selected_contact_id=contact_id,
                )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_conversion)
    mover = threading.Thread(target=reassign_contact)
    worker.start()
    mover.start()
    worker.join(timeout=15)
    mover.join(timeout=15)

    assert len(errors) == 1
    assert isinstance(errors[0], BriefConversionValidationError)
    assert "different company" in str(errors[0]).lower()

    verifier = connect()
    assert db.count(verifier, "source_records") == 0


def test_concurrent_briefs_same_email_resolve_without_http_500(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    email = "shared@acme.example"
    brief_a = db.insert_paid_brief(
        migrated_conn,
        email=email,
        website="https://acme-a.example",
    )
    brief_b = db.insert_paid_brief(
        migrated_conn,
        email=email,
        website="https://acme-b.example",
    )

    create_barrier = threading.Barrier(2, timeout=15)
    from app.repositories.postgres import PostgresContactRepository

    real_repo = PostgresContactRepository()
    original_create = real_repo.create

    def gated_create(
        conn: psycopg.Connection,
        **kwargs: Any,
    ) -> dict[str, Any]:
        create_barrier.wait()
        return original_create(conn, **kwargs)

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def run_conversion(brief: dict[str, Any]) -> None:
        conn = connect()
        service = CrmService()
        try:
            with patch.object(service._repos.contacts, "create", side_effect=gated_create):
                result = service.convert_project_brief(
                    conn,
                    brief=brief,
                    actor_context=ACTOR,
                    price_cents=20_000,
                    company_choice="new",
                    contact_choice="new",
                )
            with guard:
                results.append(result)
        except BaseException as exc:
            with guard:
                errors.append(exc)

    threads = [
        threading.Thread(target=run_conversion, args=(brief_a,)),
        threading.Thread(target=run_conversion, args=(brief_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) + len(errors) == 2
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], BriefConversionValidationError)
    assert "email already exists" in str(errors[0]).lower()

    verifier = connect()
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1
    assert db.count(verifier, "companies") == 1


def test_validation_failure_rolls_back_all_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    other = _insert_company(
        migrated_conn, db, name="Other Co", domain="other.example"
    )
    contact = _insert_contact(
        migrated_conn,
        db,
        full_name="Ops Lead",
        email="ops@acme.example",
        company_id=UUID(str(other["id"])),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    before = _counts(db, connect())

    service = CrmService()
    with pytest.raises(BriefConversionValidationError):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=UUID(str(contact["id"])),
        )

    after = _counts(db, connect())
    assert after == before


def test_unique_constraint_race_rolls_back_losing_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    email = "race@acme.example"
    brief_a = db.insert_paid_brief(migrated_conn, email=email)
    brief_b = db.insert_paid_brief(migrated_conn, email=email)
    service = CrmService()

    first = service.convert_project_brief(
        migrated_conn,
        brief=brief_a,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )
    assert first["idempotent"] is False

    with pytest.raises(BriefConversionValidationError, match="email already exists"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief_b,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    verifier = connect()
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1
    assert db.count(verifier, "companies") == 1


def test_repository_failure_rolls_back_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    brief = db.insert_paid_brief(migrated_conn)
    service = CrmService()

    with patch(
        "app.crm_service.audit_service.record_brief_convert",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.convert_project_brief(
                migrated_conn,
                brief=brief,
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )

    verifier = connect()
    for table in CRM_TABLES:
        assert db.count(verifier, table) == 0


def test_successful_conversion_is_idempotent_on_retry(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    brief = db.insert_paid_brief(migrated_conn)
    service = CrmService()

    first = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )
    second = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["company"]["id"] == first["company"]["id"]
    assert second["contact"]["id"] == first["contact"]["id"]
    assert second["source_record"]["id"] == first["source_record"]["id"]

    verifier = connect()
    assert db.count(verifier, "source_records") == 1
    assert db.count(verifier, "audit_events") == 1
