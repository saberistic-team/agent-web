"""Recovery contracts for failed import and migration (#130).

Proves that a failed LinkedIn import commit or a failed migration attempt does
not destroy prior valid CRM state. Runs against live PostgreSQL (``contract``
marker) in the isolated pg-contract workflow.
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row, tuple_row

from app.actor_context import ActorContext
from app.companies import CompanyCreate
from app.contacts import ContactCreate
from app.crm_service import CrmService
from app.linkedin_import import compute_import_checksum
from app.migrations.definitions import MIGRATIONS, Migration
from app.migrations.runner import apply_migrations

ACTOR = ActorContext(actor="operator", correlation_id="e2e-recovery-1")

CONNECTIONS = [
    {
        "profile_url": "https://linkedin.com/in/recovery-new",
        "full_name": "Recovery New",
        "title": "Engineer",
    }
]


@pytest.fixture
def recovery_env(migrated_conn: psycopg.Connection) -> Iterator[dict[str, Any]]:
    yield {"conn": migrated_conn, "crm": CrmService()}


def _seed_valid_crm_state(conn: psycopg.Connection, crm: CrmService) -> dict[str, Any]:
    company_result = crm.create_company(
        conn,
        company=CompanyCreate(
            name="Prior Valid Co",
            domain="prior-valid.example",
            website="https://prior-valid.example",
        ),
        actor_context=ACTOR,
    )
    company = company_result["company"]
    company_id = UUID(str(company["id"]))

    contact_result = crm.create_contact(
        conn,
        contact=ContactCreate(
            full_name="Prior Contact",
            email="prior@prior-valid.example",
            company_id=company_id,
        ),
        actor_context=ACTOR,
    )
    contact = contact_result["contact"]
    contact_id = UUID(str(contact["id"]))

    evidence = crm.attach_research_record(
        conn,
        actor_context=ACTOR,
        record_type="verified_fact",
        company_id=company_id,
        body="Existing evidence before failed import",
    )
    conn.commit()

    return {
        "company_id": company_id,
        "contact_id": contact_id,
        "company_name": company["name"],
        "contact_email": contact["email"],
        "evidence_id": evidence["id"],
    }


def _count_table(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


@pytest.mark.contract
def test_failed_import_preserves_prior_valid_state(recovery_env: dict[str, Any]) -> None:
    conn: psycopg.Connection = recovery_env["conn"]
    crm: CrmService = recovery_env["crm"]

    prior = _seed_valid_crm_state(conn, crm)
    counts_before = {
        "companies": _count_table(conn, "companies"),
        "contacts": _count_table(conn, "contacts"),
        "research_records": _count_table(conn, "research_records"),
        "import_batches": _count_table(conn, "import_batches"),
    }

    checksum = compute_import_checksum(CONNECTIONS)
    with patch(
        "app.crm_service.audit_service.record_import_batch",
        side_effect=RuntimeError("simulated audit failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated audit failure"):
            crm.commit_linkedin_import(
                conn,
                actor_context=ACTOR,
                connections=CONNECTIONS,
                checksum=checksum,
            )

    conn.rollback()

    counts_after = {
        "companies": _count_table(conn, "companies"),
        "contacts": _count_table(conn, "contacts"),
        "research_records": _count_table(conn, "research_records"),
        "import_batches": _count_table(conn, "import_batches"),
    }
    assert counts_after == counts_before

    company = crm.get_company(conn, prior["company_id"])
    contact = crm.get_contact(conn, prior["contact_id"])
    assert company is not None
    assert contact is not None
    assert company["name"] == prior["company_name"]
    assert contact["email"] == prior["contact_email"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM research_records WHERE id = %s",
            (prior["evidence_id"],),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["body"] == "Existing evidence before failed import"

    # No orphan import batch from the failed attempt
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM import_batches WHERE checksum = %s",
            (checksum,),
        )
        row = cur.fetchone()
    assert row is not None and int(row["n"]) == 0


@pytest.mark.contract
def test_failed_migration_preserves_prior_valid_state(
    pg_conn: psycopg.Connection,
) -> None:
    """After a failed migration, re-apply succeeds and seeded CRM rows remain."""
    apply_migrations(pg_conn)
    pg_conn.commit()
    pg_conn.row_factory = dict_row

    crm = CrmService()
    prior = _seed_valid_crm_state(pg_conn, crm)

    broken = Migration(
        version="999",
        name="broken_test_migration",
        up_sql="CREATE TABLE e2e_broken_should_rollback (id INT); INVALID SQL SYNTAX;",
    )
    extended = (*MIGRATIONS, broken)

    with pytest.raises(Exception):
        apply_migrations(pg_conn, migrations=extended)

    company = crm.get_company(pg_conn, prior["company_id"])
    assert company is not None
    assert company["name"] == prior["company_name"]

    with pg_conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations WHERE version = %s", ("999",))
        assert cur.fetchone() is None

    pg_conn.row_factory = tuple_row
    assert apply_migrations(pg_conn) == []

    pg_conn.row_factory = dict_row
    contact = crm.get_contact(pg_conn, prior["contact_id"])
    assert contact is not None
    assert contact["email"] == prior["contact_email"]
