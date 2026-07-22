"""Real-PostgreSQL proofs for #226 active/archive-aware email identity.

Issue #226 acceptance requires live-Postgres coverage of the partial unique
index ``idx_contacts_email_unique`` and the active/archive lookup split. These
tests exercise a real database (not SQL-string or mock assertions):

* the partial unique index blocks two *active* rows sharing a normalized email
  and raises a real ``UniqueViolation`` tagged with the index name;
* an active and an archived row may share an email, and archived rows may
  duplicate freely (the index only covers active, non-archived rows);
* ``get_active_by_email`` / ``get_archived_by_email`` return the expected rows
  against real data;
* the service create/update paths surface ``ContactEmailConflictError`` raised
  from a real ``UniqueViolation`` — no mocks.

Broader migration/concurrency contracts live in #228; this file only covers the
PG proofs #226 acceptance names explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.actor_context import ActorContext
from app.contacts import ContactCreate, ContactEmailConflictError, ContactUpdate
from app.crm_service import CrmService, _is_contact_email_unique_violation
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresContactRepository

ACTOR = ActorContext(actor="operator", correlation_id="corr-email-identity")

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres contact identity tests")


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


def _insert_contact(
    conn: psycopg.Connection,
    *,
    contact_id: UUID,
    full_name: str,
    email: str | None,
    archived_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO contacts (id, full_name, email, archived_at, buying_roles)
        VALUES (%s, %s, %s, %s, '{}')
        """,
        (contact_id, full_name, email, archived_at),
    )


@pytest.mark.integration
def test_partial_unique_index_blocks_two_active_duplicates(pg_conn: psycopg.Connection) -> None:
    """Two active rows with the same normalized email raise a real UniqueViolation."""
    _insert_contact(
        pg_conn,
        contact_id=uuid4(),
        full_name="Ada Lovelace",
        email="ada@example.com",
    )
    pg_conn.commit()

    # Different case must still collide because the index is on LOWER(email).
    with pytest.raises(pg_errors.UniqueViolation) as exc_info:
        _insert_contact(
            pg_conn,
            contact_id=uuid4(),
            full_name="Ada Duplicate",
            email="ADA@example.com",
        )
    assert _is_contact_email_unique_violation(exc_info.value)
    assert (exc_info.value.diag.constraint_name or "") == "idx_contacts_email_unique"
    pg_conn.rollback()

    # The blocked insert must not have created a second active row.
    row = pg_conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE LOWER(email) = %s AND archived_at IS NULL",
        ("ada@example.com",),
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


@pytest.mark.integration
def test_active_and_archived_may_share_email(pg_conn: psycopg.Connection) -> None:
    """The partial index only covers active rows, so active+archived can coexist."""
    active_id = uuid4()
    archived_id = uuid4()
    _insert_contact(
        pg_conn,
        contact_id=archived_id,
        full_name="Ada (archived)",
        email="ADA@example.com",
        archived_at=datetime.now(timezone.utc),
    )
    # No UniqueViolation expected here: the archived row is outside the index.
    _insert_contact(
        pg_conn,
        contact_id=active_id,
        full_name="Ada (active)",
        email="ada@example.com",
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    active = repo.get_active_by_email(pg_conn, "ada@example.com")
    archived = repo.get_archived_by_email(pg_conn, "ada@example.com")
    assert active is not None
    assert archived is not None
    assert active["id"] == active_id
    assert active["archived_at"] is None
    assert archived["id"] == archived_id
    assert archived["archived_at"] is not None


@pytest.mark.integration
def test_multiple_archived_duplicates_allowed_and_freshest_wins(
    pg_conn: psycopg.Connection,
) -> None:
    """Archived rows may duplicate freely; get_archived_by_email returns freshest."""
    older_id = uuid4()
    newer_id = uuid4()
    now = datetime.now(timezone.utc)
    _insert_contact(
        pg_conn,
        contact_id=older_id,
        full_name="Older Archived",
        email="dup@example.com",
        archived_at=now - timedelta(days=3),
    )
    # Second archived row with the same email must be permitted by the index.
    _insert_contact(
        pg_conn,
        contact_id=newer_id,
        full_name="Newer Archived",
        email="DUP@example.com",
        archived_at=now,
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    assert repo.get_active_by_email(pg_conn, "dup@example.com") is None
    archived = repo.get_archived_by_email(pg_conn, "dup@example.com")
    assert archived is not None
    assert archived["id"] == newer_id


@pytest.mark.integration
def test_create_contact_duplicate_active_email_raises_conflict(
    pg_conn: psycopg.Connection,
) -> None:
    """Service create path surfaces ContactEmailConflictError from a real UniqueViolation."""
    service = CrmService()
    service.create_contact(
        pg_conn,
        contact=ContactCreate(full_name="First", email="dup@example.com"),
        actor_context=ACTOR,
    )

    with pytest.raises(ContactEmailConflictError) as exc_info:
        service.create_contact(
            pg_conn,
            # Different case + name; the active partial index must still reject it.
            contact=ContactCreate(full_name="Second", email="DUP@example.com"),
            actor_context=ACTOR,
        )
    assert exc_info.value.email == "dup@example.com"
    assert isinstance(exc_info.value.__cause__, pg_errors.UniqueViolation)

    # The failed create must have rolled back — only the first active row remains.
    row = pg_conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE LOWER(email) = %s AND archived_at IS NULL",
        ("dup@example.com",),
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


@pytest.mark.integration
def test_update_contact_to_duplicate_active_email_raises_conflict(
    pg_conn: psycopg.Connection,
) -> None:
    """Service update path surfaces ContactEmailConflictError from a real UniqueViolation."""
    service = CrmService()
    service.create_contact(
        pg_conn,
        contact=ContactCreate(full_name="Owner", email="owner@example.com"),
        actor_context=ACTOR,
    )
    mover = service.create_contact(
        pg_conn,
        contact=ContactCreate(full_name="Mover", email="mover@example.com"),
        actor_context=ACTOR,
    )
    mover_id = UUID(str(mover["contact"]["id"]))

    with pytest.raises(ContactEmailConflictError) as exc_info:
        service.update_contact(
            pg_conn,
            mover_id,
            contact=ContactUpdate(full_name="Mover", email="OWNER@example.com"),
            actor_context=ACTOR,
        )
    assert exc_info.value.email == "owner@example.com"
    assert isinstance(exc_info.value.__cause__, pg_errors.UniqueViolation)

    # The mover keeps its original email after rollback.
    repo = PostgresContactRepository()
    unchanged = repo.get_by_id(pg_conn, mover_id)
    assert unchanged is not None
    assert unchanged["email"] == "mover@example.com"
