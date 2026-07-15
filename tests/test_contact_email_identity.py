"""PostgreSQL tests for active/archive-aware contact email identity (#226)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.actor_context import ActorContext
from app.brief_conversion import BriefConversionValidationError, normalize_brief_email
from app.contacts import ContactCreate, ContactEmailConflictError, ContactUpdate
from app.crm_service import CrmService
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresContactRepository

pytestmark = [pytest.mark.integration]

_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
ACTOR = ActorContext(actor="operator", correlation_id="corr-email-identity")


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if os.environ.get("REQUIRE_TEST_DATABASE") == "1":
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


def _insert_company(conn: psycopg.Connection, *, name: str) -> UUID:
    company_id = uuid4()
    conn.execute(
        """
        INSERT INTO companies (id, name, website, pipeline_stage)
        VALUES (%s, %s, %s, 'researching')
        """,
        (company_id, name, f"https://{name.lower().replace(' ', '')}.example"),
    )
    return company_id


def _insert_contact(
    conn: psycopg.Connection,
    *,
    email: str,
    full_name: str = "Pat Example",
    company_id: UUID | None = None,
    archived_at: datetime | None = None,
) -> UUID:
    contact_id = uuid4()
    conn.execute(
        """
        INSERT INTO contacts (id, full_name, email, company_id, archived_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (contact_id, full_name, email, company_id, archived_at),
    )
    return contact_id


def test_active_and_archived_email_lookups_are_separate(pg_conn: psycopg.Connection) -> None:
    repo = PostgresContactRepository()
    company_id = _insert_company(pg_conn, name="Acme")
    active_id = _insert_contact(
        pg_conn,
        email="lead@example.com",
        full_name="Active Pat",
        company_id=company_id,
    )
    archived_id = _insert_contact(
        pg_conn,
        email="lead@example.com",
        full_name="Archived Pat",
        archived_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    active = repo.get_active_by_email(pg_conn, "LEAD@Example.COM")
    archived = repo.get_archived_by_email(pg_conn, "lead@example.com")
    via_alias = repo.get_by_email(pg_conn, "lead@example.com")

    assert active is not None
    assert active["id"] == active_id
    assert active["archived_at"] is None
    assert archived is not None
    assert archived["id"] == archived_id
    assert archived["archived_at"] is not None
    assert via_alias is not None
    assert via_alias["id"] == active_id


def test_archived_only_email_lookup_returns_archived_not_active(pg_conn: psycopg.Connection) -> None:
    repo = PostgresContactRepository()
    archived_id = _insert_contact(
        pg_conn,
        email="solo@example.com",
        archived_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    assert repo.get_active_by_email(pg_conn, "solo@example.com") is None
    assert repo.get_by_email(pg_conn, "solo@example.com") is None
    archived = repo.get_archived_by_email(pg_conn, "solo@example.com")
    assert archived is not None
    assert archived["id"] == archived_id


def test_partial_unique_index_blocks_active_duplicate(pg_conn: psycopg.Connection) -> None:
    _insert_contact(pg_conn, email="dup@example.com")
    pg_conn.commit()

    with pytest.raises(pg_errors.UniqueViolation):
        _insert_contact(pg_conn, email="DUP@example.com")
        pg_conn.commit()


def test_archived_and_active_can_share_email_under_partial_unique_index(
    pg_conn: psycopg.Connection,
) -> None:
    active_id = _insert_contact(pg_conn, email="shared@example.com", full_name="Active")
    archived_id = _insert_contact(
        pg_conn,
        email="shared@example.com",
        full_name="Archived",
        archived_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    repo = PostgresContactRepository()
    assert repo.get_active_by_email(pg_conn, "shared@example.com")["id"] == active_id
    assert repo.get_archived_by_email(pg_conn, "shared@example.com")["id"] == archived_id


def test_create_contact_duplicate_raises_safe_conflict(pg_conn: psycopg.Connection) -> None:
    service = CrmService()
    _insert_contact(pg_conn, email="exists@example.com")
    pg_conn.commit()

    with pytest.raises(ContactEmailConflictError, match="already exists"):
        service.create_contact(
            pg_conn,
            contact=ContactCreate(full_name="New", email="EXISTS@example.com"),
        )


def test_update_contact_duplicate_raises_safe_conflict(pg_conn: psycopg.Connection) -> None:
    service = CrmService()
    first_id = _insert_contact(pg_conn, email="first@example.com")
    second_id = _insert_contact(pg_conn, email="second@example.com")
    pg_conn.commit()

    with pytest.raises(ContactEmailConflictError, match="already exists"):
        service.update_contact(
            pg_conn,
            second_id,
            contact=ContactUpdate(full_name="Second", email="FIRST@example.com"),
        )


def test_brief_conversion_prefers_active_over_archived_email_match(
    pg_conn: psycopg.Connection,
) -> None:
    service = CrmService()
    company_id = _insert_company(pg_conn, name="Northwind")
    active_id = _insert_contact(
        pg_conn,
        email="ops@acme.example",
        company_id=company_id,
    )
    _insert_contact(
        pg_conn,
        email="ops@acme.example",
        archived_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    pg_conn.execute(
        """
        UPDATE companies SET domain = 'acme.example', website = 'https://acme.example'
        WHERE id = %s
        """,
        (company_id,),
    )
    pg_conn.commit()

    brief = {
        "id": 42,
        "website": "https://acme.example",
        "contact_value": "Ops@Acme.Example",
        "status": "paid",
    }
    matches = service.find_brief_conversion_matches(pg_conn, brief, price_cents=20_000)

    assert len(matches["contact_matches"]) == 1
    assert matches["contact_matches"][0]["id"] == active_id
    assert matches["archived_contact_matches"] == []


def test_brief_conversion_surfaces_archived_only_match_for_review(
    pg_conn: psycopg.Connection,
) -> None:
    service = CrmService()
    archived_id = _insert_contact(
        pg_conn,
        email="archived-only@example.com",
        archived_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    brief = {
        "id": 43,
        "website": "https://acme.example",
        "contact_value": "archived-only@example.com",
        "status": "paid",
    }
    matches = service.find_brief_conversion_matches(pg_conn, brief, price_cents=20_000)

    assert matches["contact_matches"] == []
    assert len(matches["archived_contact_matches"]) == 1
    assert matches["archived_contact_matches"][0]["id"] == archived_id


def test_brief_conversion_create_succeeds_when_only_archived_shares_email(
    pg_conn: psycopg.Connection,
) -> None:
    service = CrmService()
    _insert_contact(
        pg_conn,
        email="retry@example.com",
        archived_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    brief = {
        "id": 44,
        "website": "https://retry.example",
        "contact_value": "retry@example.com",
        "status": "paid",
    }

    result = service.convert_project_brief(
        pg_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )
    pg_conn.commit()

    assert result["idempotent"] is False
    assert result["contact"]["email"] == "retry@example.com"
    assert result["contact"]["archived_at"] is None


def test_brief_conversion_is_idempotent_on_retry(pg_conn: psycopg.Connection) -> None:
    service = CrmService()
    brief = {
        "id": 45,
        "website": "https://idempotent.example",
        "contact_value": "idempotent@example.com",
        "status": "paid",
    }

    first = service.convert_project_brief(
        pg_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )
    pg_conn.commit()

    second = service.convert_project_brief(
        pg_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["contact"]["id"] == first["contact"]["id"]


def test_brief_conversion_rejects_linking_archived_contact(pg_conn: psycopg.Connection) -> None:
    service = CrmService()
    archived_id = _insert_contact(
        pg_conn,
        email="blocked@example.com",
        archived_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    pg_conn.commit()

    brief = {
        "id": 46,
        "website": "https://blocked.example",
        "contact_value": "blocked@example.com",
        "status": "paid",
    }

    with pytest.raises(BriefConversionValidationError, match="archived"):
        service.convert_project_brief(
            pg_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=archived_id,
        )


def test_brief_conversion_rejects_company_mismatch_when_linking_contact(
    pg_conn: psycopg.Connection,
) -> None:
    service = CrmService()
    company_a = _insert_company(pg_conn, name="Company A")
    company_b = _insert_company(pg_conn, name="Company B")
    contact_id = _insert_contact(
        pg_conn,
        email="mismatch@example.com",
        company_id=company_b,
    )
    pg_conn.execute(
        "UPDATE companies SET domain = 'acme.example' WHERE id = %s",
        (company_a,),
    )
    pg_conn.commit()

    brief = {
        "id": 47,
        "website": "https://acme.example",
        "contact_value": "mismatch@example.com",
        "status": "paid",
    }

    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            pg_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=company_a,
            selected_contact_id=contact_id,
        )


def test_normalize_brief_email_matches_contact_lookup_policy() -> None:
    assert normalize_brief_email("  Ops@Acme.Example ") == "ops@acme.example"
