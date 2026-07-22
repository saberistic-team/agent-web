"""Real-PostgreSQL audit contracts for research and pipeline activity (#334)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from app import audit_service
from app.acquisition_pipeline import PipelineActivityCreate
from app.actor_context import ActorContext
from app.crm_service import CrmService
from app.repositories.postgres import (
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
)

ACTOR = ActorContext(actor="operator", correlation_id="corr-audit-research-pipeline")

SECRET_BODY = "TOP_SECRET_RESEARCH_BODY_334"
SECRET_SUMMARY = "CONFIDENTIAL_ACTIVITY_SUMMARY_334"
SECRET_URL = "https://reports.example.com/2025?token=sk_live_secret_334"
SECRET_VALUE = "ceo@secret.example"
SECRET_METADATA = {"api_key": "sk_test_334", "note": "private"}


def _audit_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM audit_events ORDER BY created_at ASC, id ASC")
        return [dict(row) for row in cur.fetchall()]


def _audit_json_blob(row: dict[str, Any]) -> str:
    return json.dumps(row.get("summary_after") or {})


def _create_pipeline_company(
    companies: PostgresCompanyRepository,
    pipeline: PostgresPipelineRepository,
    conn: psycopg.Connection,
    *,
    name: str,
) -> dict[str, Any]:
    company = companies.create(conn, name=name)
    pipeline.update_pipeline_fields(
        conn,
        company["id"],
        pipeline_stage="researching",
    )
    return company


def test_research_create_persists_bounded_audit_in_postgres(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Audit Research Co")
    company_id = company["id"]
    migrated_conn.commit()

    service.attach_research_record(
        migrated_conn,
        actor_context=ACTOR,
        record_type="hypothesis",
        company_id=company_id,
        body=SECRET_BODY,
        source_url=SECRET_URL,
        observed_value=SECRET_VALUE,
        metadata=SECRET_METADATA,
    )

    rows = _audit_rows(migrated_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == audit_service.ACTION_RESEARCH_RECORD_CREATE
    assert row["actor"] == ACTOR.actor
    assert row["correlation_id"] == ACTOR.correlation_id
    blob = _audit_json_blob(row)
    assert SECRET_BODY not in blob
    assert SECRET_URL not in blob
    assert SECRET_VALUE not in blob
    assert row["summary_after"]["has_source_url"] is True


def test_pipeline_activity_persists_bounded_audit_in_postgres(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    service = CrmService()
    company = _create_pipeline_company(companies, pipeline, migrated_conn, name="Audit Pipe Co")
    company_id = company["id"]
    migrated_conn.commit()

    created = service.record_pipeline_activity(
        migrated_conn,
        actor_context=ACTOR,
        company_id=company_id,
        activity=PipelineActivityCreate(
            activity_type="outreach",
            summary=SECRET_SUMMARY,
            metadata=SECRET_METADATA,
        ),
    )

    rows = _audit_rows(migrated_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == audit_service.ACTION_PIPELINE_ACTIVITY_CREATE
    assert row["entity_id"] == str(created["id"])
    blob = _audit_json_blob(row)
    assert SECRET_SUMMARY not in blob
    assert "sk_test_334" not in blob


def test_research_create_rolls_back_on_audit_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Rollback Research Co")
    company_id = company["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_research_record_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.attach_research_record(
                migrated_conn,
                actor_context=ACTOR,
                record_type="hypothesis",
                company_id=company_id,
                body="Should not persist",
            )

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM research_records").fetchone()["n"] == 0
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


def test_pipeline_activity_rolls_back_on_audit_failure(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    service = CrmService()
    company = _create_pipeline_company(
        companies, pipeline, migrated_conn, name="Rollback Pipe Co"
    )
    company_id = company["id"]
    migrated_conn.commit()

    with patch(
        "app.crm_service.audit_service.record_pipeline_activity_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.record_pipeline_activity(
                migrated_conn,
                actor_context=ACTOR,
                company_id=company_id,
                activity=PipelineActivityCreate(activity_type="note", summary="No audit"),
            )

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 0
    assert verifier.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"] == 0


def test_concurrent_research_submissions_create_distinct_audit_events(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
) -> None:
    companies = PostgresCompanyRepository()
    company = companies.create(migrated_conn, name="Concurrent Research Co")
    company_id = company["id"]
    migrated_conn.commit()

    barrier = threading.Barrier(2, timeout=15)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        conn = connect()
        try:
            barrier.wait()
            record = CrmService().attach_research_record(
                conn,
                actor_context=ActorContext(
                    actor=ACTOR.actor,
                    correlation_id=f"corr-concurrent-{index}",
                ),
                record_type="hypothesis",
                company_id=company_id,
                body=f"Observation {index}",
            )
            with guard:
                results.append(record)
        except BaseException as exc:  # pragma: no cover
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    record_ids = {str(row["id"]) for row in results}
    assert len(record_ids) == 2

    verifier = connect()
    assert verifier.execute("SELECT COUNT(*) AS n FROM research_records").fetchone()["n"] == 2
    audit_rows = verifier.execute(
        "SELECT correlation_id, entity_id FROM audit_events ORDER BY entity_id"
    ).fetchall()
    assert len(audit_rows) == 2
    assert {row["correlation_id"] for row in audit_rows} == {
        "corr-concurrent-0",
        "corr-concurrent-1",
    }


def test_contact_research_create_persists_audit_with_contact_id(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    contacts = PostgresContactRepository()
    service = CrmService()
    company = companies.create(migrated_conn, name="Contact Research Co")
    contact = contacts.create(
        migrated_conn,
        full_name="Ada Lovelace",
        company_id=company["id"],
    )
    migrated_conn.commit()

    service.attach_research_record(
        migrated_conn,
        actor_context=ACTOR,
        record_type="relationship_context",
        company_id=company["id"],
        contact_id=contact["id"],
        body="Warm intro path",
    )

    rows = _audit_rows(migrated_conn)
    assert len(rows) == 1
    assert rows[0]["summary_after"]["contact_id"] == str(contact["id"])
