"""CRM repository SQL parsed and executed by a real PostgreSQL engine (#228).

These tests run the production repository queries — including joined contact
queries, array/`ANY` predicates, JSONB payloads, and partial-index-backed
lookups — against PostgreSQL so invalid SQL, type mismatches, or join errors
fail here instead of in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg

from app.contacts import DECISION_MAKER_BUYING_ROLES
from app.repositories.postgres import (
    PostgresAcquisitionDashboardRepository,
    PostgresActivityRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresImportBatchRepository,
    PostgresPipelineRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
)


def test_company_repository_crud(migrated_conn: psycopg.Connection) -> None:
    repo = PostgresCompanyRepository()
    created = repo.create(
        migrated_conn,
        name="Acme Robotics",
        website="https://acme.example",
        domain="acme.example",
        category="robotics",
        stage="seed",
        target_status="target",
    )
    company_id = created["id"]
    assert created["status"] == "prospect"

    fetched = repo.get_by_id(migrated_conn, company_id)
    assert fetched is not None and fetched["name"] == "Acme Robotics"

    by_domain = repo.find_by_domain(migrated_conn, "acme.example")
    assert [row["id"] for row in by_domain] == [company_id]

    listed = repo.list_all(migrated_conn, query="acme", category="robotics")
    assert any(row["id"] == company_id for row in listed)

    updated = repo.update(migrated_conn, company_id, notes="warm intro via network")
    assert updated is not None and updated["notes"] == "warm intro via network"

    archived = repo.archive(migrated_conn, company_id)
    assert archived is not None and archived["archived_at"] is not None
    assert repo.find_by_domain(migrated_conn, "acme.example") == []

    restored = repo.restore(migrated_conn, company_id)
    assert restored is not None and restored["archived_at"] is None


def test_contact_repository_crud_and_joined_queries(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    contacts = PostgresContactRepository()
    company = companies.create(migrated_conn, name="Acme", domain="acme.example")
    company_id = company["id"]

    contact = contacts.create(
        migrated_conn,
        full_name="Dana Lead",
        email="Dana.Lead@Acme.Example",
        title="CTO",
        profile_url="https://linkedin.com/in/danalead",
        company_id=company_id,
        buying_roles=["technical_buyer"],
    )
    contact_id = contact["id"]

    # get_by_email is case-insensitive (LOWER(email)).
    assert contacts.get_by_email(migrated_conn, "dana.lead@acme.example") is not None

    # get_active_by_email joins companies and returns company_name.
    active = contacts.get_active_by_email(migrated_conn, "dana.lead@acme.example")
    assert active is not None
    assert active["id"] == contact_id
    assert active["company_name"] == "Acme"

    # list_all joins companies (company_name) and filters by buying role array.
    listed = contacts.list_all(migrated_conn, buying_role="technical_buyer")
    assert any(row["id"] == contact_id for row in listed)
    assert all("company_name" in row for row in listed)

    by_profile = contacts.find_by_profile_url(
        migrated_conn, "https://linkedin.com/in/danalead"
    )
    assert [row["id"] for row in by_profile] == [contact_id]

    by_name = contacts.find_by_name_company(
        migrated_conn, full_name="dana lead", company_id=company_id
    )
    assert [row["id"] for row in by_name] == [contact_id]

    for_company = contacts.list_for_company(migrated_conn, company_id)
    assert [row["id"] for row in for_company] == [contact_id]

    archived = contacts.archive(migrated_conn, contact_id)
    assert archived is not None and archived["archived_at"] is not None
    # Archived contacts are not returned by the active-aware lookup.
    assert contacts.get_active_by_email(migrated_conn, "dana.lead@acme.example") is None
    restored = contacts.restore(migrated_conn, contact_id)
    assert restored is not None and restored["archived_at"] is None


def test_source_record_repository_roundtrip(migrated_conn: psycopg.Connection) -> None:
    repo = PostgresSourceRecordRepository()
    record = repo.create(
        migrated_conn,
        source_type="project_brief",
        external_id="4242",
        payload={"pipeline_stage": "diagnostic_paid", "brief_id": 4242},
    )
    assert record["payload"] == {"pipeline_stage": "diagnostic_paid", "brief_id": 4242}

    found = repo.get_by_source(
        migrated_conn, source_type="project_brief", external_id="4242"
    )
    assert found is not None and found["id"] == record["id"]
    assert (
        repo.get_by_source(migrated_conn, source_type="project_brief", external_id="0")
        is None
    )


def test_activity_repository_listing(migrated_conn: psycopg.Connection) -> None:
    companies = PostgresCompanyRepository()
    activities = PostgresActivityRepository()
    company = companies.create(migrated_conn, name="Acme")
    company_id = company["id"]

    activities.create(
        migrated_conn,
        activity_type="note",
        summary="First touch",
        company_id=company_id,
        metadata={"channel": "email"},
    )
    activities.create(
        migrated_conn,
        activity_type="status_change",
        summary="Moved to qualified",
        company_id=company_id,
    )
    rows = activities.list_for_company(migrated_conn, company_id)
    assert len(rows) == 2
    # Both rows share NOW() inside one transaction, so assert membership rather
    # than tie-broken ordering.
    assert {row["summary"] for row in rows} == {"First touch", "Moved to qualified"}


def test_pipeline_repository_fields_and_history(
    migrated_conn: psycopg.Connection,
) -> None:
    companies = PostgresCompanyRepository()
    pipeline = PostgresPipelineRepository()
    company = companies.create(migrated_conn, name="Pipeline Co")
    company_id = company["id"]

    updated = pipeline.update_pipeline_fields(
        migrated_conn,
        company_id,
        pipeline_stage="qualified",
        next_action="Send diagnostic proposal",
        next_action_due_at=datetime.now(timezone.utc) - timedelta(days=1),
        pipeline_owner="Alex",
        expected_value_cents=50_000,
    )
    assert updated is not None
    assert updated["pipeline_stage"] == "qualified"
    assert updated["expected_value_cents"] == 50_000

    history = pipeline.record_stage_history(
        migrated_conn,
        company_id=company_id,
        from_stage="researching",
        to_stage="qualified",
        changed_by="alex",
        metadata={"reason": "engaged"},
    )
    assert history["to_stage"] == "qualified"
    listed = pipeline.list_stage_history(migrated_conn, company_id)
    assert any(row["id"] == history["id"] for row in listed)

    assert any(
        row["id"] == company_id
        for row in pipeline.list_companies(migrated_conn, pipeline_stage="qualified")
    )
    assert pipeline.get_company_pipeline(migrated_conn, company_id) is not None
    counts = dict(pipeline.count_by_pipeline_stage(migrated_conn))
    assert counts.get("qualified") == 1

    overdue = pipeline.list_overdue_next_actions(
        migrated_conn, reference=datetime.now(timezone.utc), limit=10
    )
    assert any(row["id"] == company_id for row in overdue)


def test_research_record_repository(migrated_conn: psycopg.Connection) -> None:
    companies = PostgresCompanyRepository()
    research = PostgresResearchRecordRepository()
    company = companies.create(migrated_conn, name="Research Co")
    company_id = company["id"]

    record = research.create(
        migrated_conn,
        record_type="verified_fact",
        company_id=company_id,
        body="Raised a Series A",
        source_name="TechCrunch",
        confidence=0.9,
        metadata={"link": "https://example.com"},
    )
    assert record["confidence"] is not None
    listed = research.list_for_company(migrated_conn, company_id)
    assert [row["id"] for row in listed] == [record["id"]]


def test_import_batch_repository(migrated_conn: psycopg.Connection) -> None:
    repo = PostgresImportBatchRepository()
    batch = repo.create(
        migrated_conn,
        source_type="linkedin",
        schema_version="1",
        checksum="abc123",
        actor="operator",
        status="committed",
        correlation_id="corr-import",
        summary_counts={"inserted": 1},
    )
    batch_id = batch["id"]
    repo.create_row(
        migrated_conn,
        batch_id=batch_id,
        row_index=0,
        source_kind="connection",
        source_identity={"profile_url": "https://linkedin.com/in/x"},
        outcome="inserted",
        entity_type="contact",
        entity_id=uuid4(),
        applied_snapshot={"full_name": "X"},
    )
    updated = repo.update_status(
        migrated_conn, batch_id, status="committed", summary_counts={"inserted": 1}
    )
    assert updated is not None

    found = repo.get_committed_by_checksum(migrated_conn, "abc123")
    assert found is not None and found["id"] == batch_id
    rows = repo.list_rows_for_batch(migrated_conn, batch_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "inserted"


def test_acquisition_dashboard_joined_queries(
    migrated_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    companies = PostgresCompanyRepository()
    contacts = PostgresContactRepository()
    dashboard = PostgresAcquisitionDashboardRepository()

    target = companies.create(
        migrated_conn,
        name="Target Co",
        category="fintech",
        stage="seed",
        target_status="target",
    )
    contacts.create(
        migrated_conn,
        full_name="Buyer One",
        email="buyer@target.example",
        company_id=target["id"],
        buying_roles=list(DECISION_MAKER_BUYING_ROLES)[:1],
    )
    # A second target with no decision-maker contact.
    companies.create(
        migrated_conn,
        name="No Buyer Co",
        category="fintech",
        target_status="target",
    )

    contacts_by_dim = dict(
        dashboard.count_contacts_by_company_dimension(migrated_conn, "category")
    )
    assert contacts_by_dim.get("fintech") == 1

    without_dm = dashboard.list_companies_without_decision_maker(
        migrated_conn, limit=10
    )
    names = {row["name"] for row in without_dm}
    assert "No Buyer Co" in names
    assert "Target Co" not in names
