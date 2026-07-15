"""Commit/rollback contracts proven with real PostgreSQL connections (#228).

Results are verified from a *separate* connection so the assertions reflect
committed database state rather than an in-transaction view, and a forced
failure proves a conversion leaves no partial CRM/audit rows behind.
"""

from __future__ import annotations

from typing import Callable
from unittest.mock import patch

import psycopg
import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmService

ACTOR = ActorContext(actor="operator", correlation_id="corr-contract-tx")
CRM_TABLES = (
    "companies",
    "contacts",
    "source_records",
    "activities",
    "pipeline_stage_history",
    "audit_events",
)


def test_record_company_with_contact_commits_across_connections(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    service = CrmService()
    result = service.record_company_with_contact(
        migrated_conn,
        company_name="Acme",
        website="https://acme.example",
        contact_email="lead@acme.example",
    )
    company_id = result["company"]["id"]

    # A brand-new connection must see the committed rows.
    verifier = connect()
    company = db.fetch_dict(
        verifier, "SELECT * FROM companies WHERE id = %s", (company_id,)
    )
    assert company is not None and company["name"] == "Acme"
    assert db.count(verifier, "contacts") == 1


def test_successful_conversion_commits_full_record_set(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    brief = db.insert_paid_brief(migrated_conn)
    service = CrmService()
    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )
    assert result["idempotent"] is False
    assert result["pipeline_stage"] == "diagnostic_paid"

    verifier = connect()
    assert db.count(verifier, "companies") == 1
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1
    assert db.count(verifier, "activities") == 1
    assert db.count(verifier, "pipeline_stage_history") == 1
    assert db.count(verifier, "audit_events") == 1


def test_failed_conversion_leaves_no_partial_rows(
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

    # The whole conversion transaction rolled back on the real connection: a
    # separate connection sees no company, contact, source, activity, history,
    # or audit rows.
    verifier = connect()
    for table in CRM_TABLES:
        assert db.count(verifier, table) == 0, f"{table} retained partial rows"
    # The brief itself (committed before conversion) is untouched.
    assert db.count(verifier, "project_briefs") == 1
