"""PostgreSQL integration tests for contact restore email conflict lookup (#227)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from app.actor_context import ActorContext
from app.crm_service import CrmService
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresCompanyRepository, PostgresContactRepository

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

COMPANY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
ARCHIVED_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTIVE_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
LOWER_DUP_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
HIGHER_DUP_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
NO_EMAIL_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres contact restore tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


def _actor() -> ActorContext:
    return ActorContext(actor="operator", correlation_id="corr-restore-pg")


def _seed_company(conn: psycopg.Connection) -> dict:
    company = PostgresCompanyRepository().create(conn, name="Acme Corp")
    conn.commit()
    return company


def _insert_contact(
    conn: psycopg.Connection,
    *,
    contact_id: UUID,
    full_name: str,
    email: str | None,
    company_id: UUID | None = None,
    archived_at: datetime | None = None,
) -> dict:
    conn.execute(
        """
        INSERT INTO contacts (id, full_name, email, company_id, archived_at, buying_roles)
        VALUES (%s, %s, %s, %s, %s, '{}')
        RETURNING *
        """,
        (contact_id, full_name, email, company_id, archived_at),
    )
    row = conn.execute(
        "SELECT * FROM contacts WHERE id = %s",
        (contact_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.integration
def test_get_active_by_email_executes_with_company_join(pg_conn: psycopg.Connection) -> None:
    company = _seed_company(pg_conn)
    archived_at = datetime.now(timezone.utc)
    _insert_contact(
        pg_conn,
        contact_id=ARCHIVED_ID,
        full_name="Ada Lovelace",
        email="ada@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=archived_at,
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    result = repo.get_active_by_email(
        pg_conn,
        "ada@example.com",
        exclude_contact_id=ARCHIVED_ID,
    )
    assert result is None


@pytest.mark.integration
def test_get_active_by_email_finds_active_conflict(pg_conn: psycopg.Connection) -> None:
    company = _seed_company(pg_conn)
    _insert_contact(
        pg_conn,
        contact_id=ACTIVE_ID,
        full_name="Ada Lovelace (current)",
        email="ada@example.com",
        company_id=UUID(str(company["id"])),
    )
    _insert_contact(
        pg_conn,
        contact_id=ARCHIVED_ID,
        full_name="Ada Lovelace",
        email="ADA@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=datetime.now(timezone.utc),
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    result = repo.get_active_by_email(
        pg_conn,
        "ada@example.com",
        exclude_contact_id=ARCHIVED_ID,
    )
    assert result is not None
    assert result["id"] == ACTIVE_ID
    assert result["company_name"] == "Acme Corp"


@pytest.mark.integration
def test_get_active_by_email_excludes_self_and_archived_matches(pg_conn: psycopg.Connection) -> None:
    company = _seed_company(pg_conn)
    archived_at = datetime.now(timezone.utc)
    _insert_contact(
        pg_conn,
        contact_id=ARCHIVED_ID,
        full_name="Only Archived",
        email="solo@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=archived_at,
    )
    _insert_contact(
        pg_conn,
        contact_id=ACTIVE_ID,
        full_name="Also Archived",
        email="solo@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=archived_at,
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    assert repo.get_active_by_email(pg_conn, "solo@example.com") is None
    assert (
        repo.get_active_by_email(
            pg_conn,
            "solo@example.com",
            exclude_contact_id=ARCHIVED_ID,
        )
        is None
    )


@pytest.mark.integration
def test_get_active_by_email_returns_lowest_id_deterministically(
    pg_conn: psycopg.Connection,
) -> None:
    pg_conn.execute("DROP INDEX IF EXISTS idx_contacts_email_unique")
    _insert_contact(
        pg_conn,
        contact_id=HIGHER_DUP_ID,
        full_name="Later Duplicate",
        email="dup@example.com",
    )
    _insert_contact(
        pg_conn,
        contact_id=LOWER_DUP_ID,
        full_name="Earlier Duplicate",
        email="dup@example.com",
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    first = repo.get_active_by_email(pg_conn, "dup@example.com")
    second = repo.get_active_by_email(pg_conn, "DUP@example.com")
    assert first is not None
    assert second is not None
    assert first["id"] == LOWER_DUP_ID
    assert second["id"] == LOWER_DUP_ID


@pytest.mark.integration
def test_restore_contact_succeeds_without_email_conflict(pg_conn: psycopg.Connection) -> None:
    company = _seed_company(pg_conn)
    _insert_contact(
        pg_conn,
        contact_id=ARCHIVED_ID,
        full_name="Unique Restore",
        email="unique@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=datetime.now(timezone.utc),
    )
    pg_conn.commit()

    service = CrmService()
    result = service.restore_contact(pg_conn, ARCHIVED_ID, actor_context=_actor())
    assert result.outcome == "success"
    assert result.contact is not None
    assert result.contact["archived_at"] is None


@pytest.mark.integration
def test_restore_contact_reports_active_email_conflict(pg_conn: psycopg.Connection) -> None:
    company = _seed_company(pg_conn)
    _insert_contact(
        pg_conn,
        contact_id=ACTIVE_ID,
        full_name="Active Ada",
        email="ada@example.com",
        company_id=UUID(str(company["id"])),
    )
    _insert_contact(
        pg_conn,
        contact_id=ARCHIVED_ID,
        full_name="Archived Ada",
        email="ADA@example.com",
        company_id=UUID(str(company["id"])),
        archived_at=datetime.now(timezone.utc),
    )
    pg_conn.commit()

    service = CrmService()
    result = service.restore_contact(pg_conn, ARCHIVED_ID, actor_context=_actor())
    assert result.outcome == "conflict"
    assert result.conflicting_contact is not None
    assert result.conflicting_contact.contact_id == str(ACTIVE_ID)

    still_archived = PostgresContactRepository().get_by_id(pg_conn, ARCHIVED_ID)
    assert still_archived is not None
    assert still_archived["archived_at"] is not None


@pytest.mark.integration
def test_restore_contact_without_email_skips_conflict_lookup(
    pg_conn: psycopg.Connection,
) -> None:
    _insert_contact(
        pg_conn,
        contact_id=NO_EMAIL_ID,
        full_name="No Email Contact",
        email=None,
        archived_at=datetime.now(timezone.utc),
    )
    pg_conn.commit()

    service = CrmService()
    result = service.restore_contact(pg_conn, NO_EMAIL_ID, actor_context=_actor())
    assert result.outcome == "success"
    assert result.contact is not None
    assert result.contact["archived_at"] is None
