"""Real PostgreSQL constraint contracts for CRM tables (#228).

Partial active-email uniqueness, source-record uniqueness, foreign keys, and
check constraints are verified by provoking the real database errors — not by
mocking ``psycopg`` diagnostics.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors

from app.repositories.postgres import (
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
)


def test_active_email_partial_uniqueness_is_case_insensitive(
    migrated_conn: psycopg.Connection,
) -> None:
    contacts = PostgresContactRepository()
    contacts.create(migrated_conn, full_name="First", email="dup@example.com")
    migrated_conn.commit()

    with pytest.raises(pg_errors.UniqueViolation) as exc:
        # LOWER(email) collides with the existing active row.
        contacts.create(migrated_conn, full_name="Second", email="DUP@Example.com")
    assert exc.value.diag.constraint_name == "idx_contacts_email_unique"
    migrated_conn.rollback()


def test_archived_contact_frees_the_active_email(
    migrated_conn: psycopg.Connection,
) -> None:
    contacts = PostgresContactRepository()
    first = contacts.create(migrated_conn, full_name="First", email="shared@example.com")
    contacts.archive(migrated_conn, first["id"])
    migrated_conn.commit()

    # The partial index excludes archived rows, so a new active row is allowed.
    second = contacts.create(
        migrated_conn, full_name="Second", email="shared@example.com"
    )
    migrated_conn.commit()
    assert second["id"] != first["id"]

    # But two *active* rows still collide.
    with pytest.raises(pg_errors.UniqueViolation):
        contacts.create(migrated_conn, full_name="Third", email="shared@example.com")
    migrated_conn.rollback()


def test_null_emails_do_not_collide(migrated_conn: psycopg.Connection) -> None:
    contacts = PostgresContactRepository()
    contacts.create(migrated_conn, full_name="No Email A", email=None)
    contacts.create(migrated_conn, full_name="No Email B", email=None)
    migrated_conn.commit()
    rows = contacts.list_all(migrated_conn)
    assert sum(1 for row in rows if row["email"] is None) == 2


def test_source_record_type_external_uniqueness(
    migrated_conn: psycopg.Connection,
) -> None:
    repo = PostgresSourceRecordRepository()
    repo.create(migrated_conn, source_type="project_brief", external_id="777")
    migrated_conn.commit()

    with pytest.raises(pg_errors.UniqueViolation) as exc:
        repo.create(migrated_conn, source_type="project_brief", external_id="777")
    assert exc.value.diag.constraint_name == "source_records_type_external_unique"
    migrated_conn.rollback()


def test_contact_company_foreign_key_enforced(
    migrated_conn: psycopg.Connection,
) -> None:
    contacts = PostgresContactRepository()
    with pytest.raises(pg_errors.ForeignKeyViolation):
        contacts.create(
            migrated_conn,
            full_name="Orphan",
            email="orphan@example.com",
            company_id=uuid4(),
        )
    migrated_conn.rollback()


def test_research_record_requires_existing_company(
    migrated_conn: psycopg.Connection,
) -> None:
    research = PostgresResearchRecordRepository()
    with pytest.raises(pg_errors.ForeignKeyViolation):
        research.create(
            migrated_conn,
            record_type="verified_fact",
            company_id=uuid4(),
            body="dangling reference",
        )
    migrated_conn.rollback()


def test_pipeline_stage_check_constraint_enforced(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    company = companies.create(migrated_conn, name="Check Co")
    migrated_conn.commit()

    with pytest.raises(pg_errors.CheckViolation):
        pipeline.update_pipeline_fields(
            migrated_conn, company["id"], pipeline_stage="not_a_real_stage"
        )
    migrated_conn.rollback()
