"""PostgreSQL contracts for brief-conversion contact/company consistency (#274).

Exercises real separate connections, ``SELECT … FOR UPDATE`` contact locking,
company-association validation, email-uniqueness races, and rollback/idempotency
on a live Postgres engine — not mocks or sequential-only calls.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest

from app.actor_context import ActorContext
from app.brief_conversion import BriefConversionValidationError
from app.brief_conversion_lock import acquire_brief_conversion_lock
from app.crm_service import CrmService
from app.repositories.postgres import PostgresContactRepository

ACTOR = ActorContext(actor="operator", correlation_id="corr-contract-274")
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
    *,
    company_id: UUID,
    name: str = "Acme",
    website: str = "https://acme.example",
    domain: str = "acme.example",
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO companies (id, name, website, domain, pipeline_stage)
        VALUES (%s, %s, %s, %s, 'researching')
        RETURNING *
        """,
        (company_id, name, website, domain),
    ).fetchone()
    assert row is not None
    conn.commit()
    return dict(row)


def _insert_contact(
    conn: psycopg.Connection,
    *,
    contact_id: UUID,
    email: str,
    company_id: UUID | None = None,
    full_name: str = "Ops Lead",
    archived_at: Any = None,
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO contacts (id, full_name, email, company_id, archived_at, buying_roles)
        VALUES (%s, %s, %s, %s, %s, '{}')
        RETURNING *
        """,
        (contact_id, full_name, email, company_id, archived_at),
    ).fetchone()
    assert row is not None
    conn.commit()
    return dict(row)


def _assert_no_conversion_artifacts(verifier: psycopg.Connection, db) -> None:
    """Conversion failures must not leave source, activity, history, or audit rows."""
    assert db.count(verifier, "source_records") == 0
    assert db.count(verifier, "activities") == 0
    assert db.count(verifier, "pipeline_stage_history") == 0
    assert db.count(verifier, "audit_events") == 0


def test_rejects_new_company_with_contact_assigned_elsewhere(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    other_company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=other_company_id, name="Other Co")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=other_company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=contact_id,
        )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)
    assert db.count(verifier, "project_briefs") == 1


def test_rejects_existing_company_with_mismatched_contact(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    target_company_id = uuid4()
    other_company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=target_company_id, name="Target Co")
    _insert_company(
        migrated_conn,
        company_id=other_company_id,
        name="Other Co",
        website="https://other.example",
        domain="other.example",
    )
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=other_company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=target_company_id,
            selected_contact_id=contact_id,
        )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)


def test_attaches_unassigned_contact_to_new_company(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    contact_id = uuid4()
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=None,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    result = CrmService().convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="existing",
        selected_contact_id=contact_id,
    )

    assert result["idempotent"] is False
    verifier = connect()
    contact = db.fetch_dict(verifier, "SELECT * FROM contacts WHERE id = %s", (contact_id,))
    assert contact is not None
    assert contact["company_id"] == result["company"]["id"]
    assert db.count(verifier, "source_records") == 1


def test_attaches_unassigned_contact_to_existing_company(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=company_id, name="Acme")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=None,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    result = CrmService().convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=company_id,
        selected_contact_id=contact_id,
    )

    verifier = connect()
    contact = db.fetch_dict(verifier, "SELECT * FROM contacts WHERE id = %s", (contact_id,))
    assert contact is not None
    assert contact["company_id"] == company_id
    assert result["company"]["id"] == company_id
    assert result["contact"]["id"] == contact_id


def test_matching_existing_company_and_contact_convert_consistently(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=company_id, name="Acme")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    result = CrmService().convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=company_id,
        selected_contact_id=contact_id,
    )

    verifier = connect()
    source = db.fetch_dict(
        verifier,
        "SELECT * FROM source_records WHERE external_id = %s",
        (str(brief["id"]),),
    )
    assert source is not None
    assert source["company_id"] == company_id
    assert source["contact_id"] == contact_id
    contact = db.fetch_dict(verifier, "SELECT * FROM contacts WHERE id = %s", (contact_id,))
    assert contact is not None
    assert contact["company_id"] == company_id
    assert result["pipeline_stage"] == "diagnostic_paid"


def test_rejects_contact_archived_between_preview_and_submission(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=company_id, name="Acme")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    archiver = connect()
    archiver.execute(
        "UPDATE contacts SET archived_at = NOW() WHERE id = %s",
        (contact_id,),
    )
    archiver.commit()

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="No existing contact matches"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=company_id,
            selected_contact_id=contact_id,
        )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)


def test_rejects_contact_reassigned_between_preview_and_submission(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    target_company_id = uuid4()
    other_company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=target_company_id, name="Target Co")
    _insert_company(
        migrated_conn,
        company_id=other_company_id,
        name="Other Co",
        website="https://other.example",
        domain="other.example",
    )
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=None,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    reassigner = connect()
    reassigner.execute(
        "UPDATE contacts SET company_id = %s WHERE id = %s",
        (other_company_id, contact_id),
    )
    reassigner.commit()

    service = CrmService()
    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=target_company_id,
            selected_contact_id=contact_id,
        )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)


def test_concurrent_distinct_briefs_same_email_resolve_deterministically(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    email = "shared@acme.example"
    brief_a = db.insert_paid_brief(
        migrated_conn,
        website="https://acme.example",
        email=email,
        brief="Brief A",
    )
    brief_b = db.insert_paid_brief(
        migrated_conn,
        website="https://acme.example",
        email=email,
        brief="Brief B",
    )

    create_barrier = threading.Barrier(2, timeout=15)
    original_create = PostgresContactRepository.create

    def gated_create(
        self: PostgresContactRepository,
        conn: psycopg.Connection,
        **kwargs: Any,
    ) -> dict[str, Any]:
        create_barrier.wait()
        return original_create(self, conn, **kwargs)

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def run_conversion(brief: dict[str, Any]) -> None:
        conn = connect()
        try:
            result = CrmService().convert_project_brief(
                conn,
                brief=brief,
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )
            with guard:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            with guard:
                errors.append(exc)

    with patch.object(PostgresContactRepository, "create", gated_create):
        threads = [
            threading.Thread(target=run_conversion, args=(brief_a,)),
            threading.Thread(target=run_conversion, args=(brief_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], BriefConversionValidationError)

    verifier = connect()
    assert db.count(verifier, "companies") == 1
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1


def test_validation_failure_rolls_back_all_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    other_company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=other_company_id, name="Other Co")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=other_company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    with pytest.raises(BriefConversionValidationError):
        CrmService().convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=contact_id,
        )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)


def test_source_unique_violation_rolls_back_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    brief = db.insert_paid_brief(migrated_conn)
    service = CrmService()

    lock_barrier = threading.Barrier(2, timeout=15)

    def gated_acquire(conn: psycopg.Connection, brief_id: int) -> None:
        lock_barrier.wait()
        acquire_brief_conversion_lock(conn, brief_id)

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def run_conversion() -> None:
        conn = connect()
        try:
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
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            with guard:
                errors.append(exc)

    with patch("app.crm_service.acquire_brief_conversion_lock", gated_acquire):
        threads = [threading.Thread(target=run_conversion) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    assert sum(1 for r in results if not r["idempotent"]) == 1
    assert sum(1 for r in results if r["idempotent"]) == 1

    verifier = connect()
    assert db.count(verifier, "companies") == 1
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1


def test_repository_failure_rolls_back_conversion_writes(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=company_id, name="Acme")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")

    with patch(
        "app.crm_service.audit_service.record_brief_convert",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            CrmService().convert_project_brief(
                migrated_conn,
                brief=brief,
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="existing",
                contact_choice="existing",
                selected_company_id=company_id,
                selected_contact_id=contact_id,
            )

    verifier = connect()
    _assert_no_conversion_artifacts(verifier, db)


def test_idempotent_retry_returns_original_conversion(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    company_id = uuid4()
    contact_id = uuid4()
    _insert_company(migrated_conn, company_id=company_id, name="Acme")
    _insert_contact(
        migrated_conn,
        contact_id=contact_id,
        email="ops@acme.example",
        company_id=company_id,
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    service = CrmService()

    first = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=company_id,
        selected_contact_id=contact_id,
    )
    second = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="existing",
        contact_choice="existing",
        selected_company_id=company_id,
        selected_contact_id=contact_id,
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert str(first["source_record"]["id"]) == str(second["source_record"]["id"])

    verifier = connect()
    assert db.count(verifier, "companies") == 1
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1


def test_archived_contact_never_silently_linked_on_convert(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    """Creating a new active contact with archived history requires explicit ack (#276)."""
    from datetime import datetime, timezone

    archived_id = uuid4()
    _insert_contact(
        migrated_conn,
        contact_id=archived_id,
        email="ops@acme.example",
        full_name="Ops Lead (archived)",
        archived_at=datetime.now(timezone.utc),
    )
    brief = db.insert_paid_brief(migrated_conn, email="ops@acme.example")
    service = CrmService()

    preview = service.find_brief_conversion_matches(
        migrated_conn,
        brief,
        price_cents=20_000,
    )
    assert preview["contact_matches"] == []
    assert preview["archived_contact_match"]["id"] == archived_id

    with pytest.raises(BriefConversionValidationError, match="Acknowledge the archived"):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
        acknowledge_archived_contact=True,
    )

    new_contact_id = UUID(str(result["contact"]["id"]))
    assert new_contact_id != archived_id

    verifier = connect()
    archived = db.fetch_dict(
        verifier,
        "SELECT * FROM contacts WHERE id = %s",
        (archived_id,),
    )
    assert archived is not None
    assert archived["archived_at"] is not None
    new_contact = db.fetch_dict(
        verifier,
        "SELECT * FROM contacts WHERE id = %s",
        (new_contact_id,),
    )
    assert new_contact is not None
    assert new_contact["archived_at"] is None
    assert new_contact["email"] == "ops@acme.example"
