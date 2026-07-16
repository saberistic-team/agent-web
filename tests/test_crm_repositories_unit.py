"""Unit tests for CRM Postgres repositories (no live Postgres)."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresIcpScoringRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
)


COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
SOURCE_ID = UUID("33333333-3333-3333-3333-333333333333")
ADMIN_ID = UUID("44444444-4444-4444-4444-444444444444")


def _mock_conn(row: dict | list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(row, list):
        cur.fetchall.return_value = row
    elif row is not None:
        cur.fetchone.return_value = row
    return conn


@pytest.mark.unit
def test_company_repository_crud() -> None:
    repo = PostgresCompanyRepository()
    created = {
        "id": COMPANY_ID,
        "name": "Acme",
        "website": "https://acme.dev",
        "status": "prospect",
    }
    conn = _mock_conn(created)

    result = repo.create(conn, name="Acme", website="https://acme.dev")
    assert result["name"] == "Acme"
    conn.commit.assert_not_called()
    insert_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO companies" in insert_sql

    conn2 = _mock_conn(created)
    assert repo.get_by_id(conn2, COMPANY_ID)["id"] == COMPANY_ID

    updated = {**created, "status": "active"}
    conn3 = _mock_conn(updated)
    row = repo.update(conn3, COMPANY_ID, status="active")
    assert row is not None
    conn3.commit.assert_not_called()
    update_sql = str(conn3.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE companies" in update_sql
    assert "updated_at" in update_sql

    conn4 = _mock_conn([created])
    companies = repo.list_all(conn4, limit=10)
    assert len(companies) == 1


@pytest.mark.unit
def test_contact_repository_create_and_lookup() -> None:
    repo = PostgresContactRepository()
    row = {
        "id": CONTACT_ID,
        "email": "lead@example.com",
        "full_name": "Lead",
        "company_id": COMPANY_ID,
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        full_name="Lead",
        email="lead@example.com",
        company_id=COMPANY_ID,
    )
    assert created["email"] == "lead@example.com"
    conn.commit.assert_not_called()

    conn3 = _mock_conn(row)
    assert repo.get_active_by_email(conn3, "lead@example.com")["id"] == CONTACT_ID
    active_sql = str(conn3.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "c.archived_at IS NULL" in active_sql
    assert "LOWER(c.email)" in active_sql
    assert "ORDER BY c.id ASC" in active_sql
    assert "LIMIT 1" in active_sql

    conn3b = _mock_conn(row)
    assert (
        repo.get_active_by_email(conn3b, "lead@example.com", exclude_contact_id=CONTACT_ID)["id"]
        == CONTACT_ID
    )
    active_excl_sql = str(conn3b.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "id <> %s" in active_excl_sql

    # Archived lookup is a separate, explicit operation (#226).
    # The real-Postgres proofs #226 requires — that the partial unique index
    # `idx_contacts_email_unique` permits an active row to coexist with an
    # archived row sharing the same email while blocking two active rows, and
    # that get_active_by_email / get_archived_by_email return the right rows
    # against real data — live in tests/test_contact_email_identity_pg.py. The
    # broader migration/concurrency contract suite is tracked in #228.
    conn3c = _mock_conn(row)
    assert repo.get_archived_by_email(conn3c, "lead@example.com")["id"] == CONTACT_ID
    archived_sql = str(conn3c.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "archived_at IS NOT NULL" in archived_sql
    assert "LIMIT 1" in archived_sql

    conn4 = _mock_conn([row])
    contacts = repo.list_for_company(conn4, COMPANY_ID, limit=10)
    assert len(contacts) == 1


@pytest.mark.unit
def test_source_record_repository_links_external_id() -> None:
    repo = PostgresSourceRecordRepository()
    row = {
        "id": SOURCE_ID,
        "source_type": "project_brief",
        "external_id": "42",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "payload": {"brief": "hello"},
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        source_type="project_brief",
        external_id="42",
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
        payload={"brief": "hello"},
    )
    assert created["external_id"] == "42"
    conn.commit.assert_not_called()

    conn2 = _mock_conn(row)
    found = repo.get_by_source(conn2, source_type="project_brief", external_id="42")
    assert found is not None
    lookup_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "source_type" in lookup_sql and "external_id" in lookup_sql


@pytest.mark.unit
def test_activity_repository_create_and_list() -> None:
    repo = PostgresActivityRepository()
    row = {
        "id": UUID("55555555-5555-5555-5555-555555555555"),
        "activity_type": "note",
        "summary": "Initial outreach",
        "company_id": COMPANY_ID,
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        activity_type="note",
        summary="Initial outreach",
        company_id=COMPANY_ID,
    )
    assert created["summary"] == "Initial outreach"
    conn.commit.assert_not_called()

    conn2 = _mock_conn([row])
    activities = repo.list_for_company(conn2, COMPANY_ID, limit=10)
    assert len(activities) == 1
    list_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY created_at DESC" in list_sql


@pytest.mark.unit
def test_admin_user_repository_create_and_lookup() -> None:
    repo = PostgresAdminUserRepository()
    row = {
        "id": ADMIN_ID,
        "email": "admin@saberistic.com",
        "display_name": "Admin",
        "role": "admin",
        "is_active": True,
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        email="admin@saberistic.com",
        display_name="Admin",
        role="admin",
    )
    assert created["role"] == "admin"
    conn.commit.assert_not_called()

    conn2 = _mock_conn(row)
    assert repo.get_by_email(conn2, "admin@saberistic.com")["id"] == ADMIN_ID

    conn3 = _mock_conn(row)
    assert repo.get_by_id(conn3, ADMIN_ID)["email"] == "admin@saberistic.com"


@pytest.mark.unit
def test_research_record_repository_create_and_list() -> None:
    repo = PostgresResearchRecordRepository()
    record_id = UUID("66666666-6666-6666-6666-666666666666")
    row = {
        "id": record_id,
        "record_type": "verified_fact",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "body": "Series B",
        "source_name": "News",
        "source_url": "https://news.example.com",
        "observed_value": "$10M",
        "observed_at": None,
        "confidence": 0.9,
        "review_at": None,
        "expires_at": None,
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        record_type="verified_fact",
        company_id=COMPANY_ID,
        body="Series B",
        contact_id=CONTACT_ID,
        source_name="News",
        source_url="https://news.example.com",
        observed_value="$10M",
        confidence=0.9,
    )
    assert created["record_type"] == "verified_fact"
    insert_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO research_records" in insert_sql
    conn.commit.assert_not_called()

    conn2 = _mock_conn([row])
    company_records = repo.list_for_company(conn2, COMPANY_ID, limit=10)
    assert len(company_records) == 1
    list_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY observed_at DESC" in list_sql

    conn3 = _mock_conn([row])
    contact_records = repo.list_for_contact(conn3, CONTACT_ID, limit=10)
    assert len(contact_records) == 1


VERSION_ID = UUID("99999999-9999-9999-9999-999999999901")


@pytest.mark.unit
@pytest.mark.integration
def test_icp_scoring_repository_version_queries() -> None:
    repo = PostgresIcpScoringRepository()
    version_row = {
        "id": VERSION_ID,
        "version_number": 1,
        "label": "Default Saberistic ICP",
        "is_active": True,
        "created_at": None,
        "created_by": "migration",
    }

    conn_active = _mock_conn(version_row)
    active = repo.get_active_version(conn_active)
    assert active is not None
    assert active["version_number"] == 1
    active_sql = str(conn_active.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "icp_scoring_versions" in active_sql
    assert "is_active = TRUE" in active_sql

    conn_by_number = _mock_conn(version_row)
    by_number = repo.get_version_by_number(conn_by_number, 1)
    assert by_number is not None
    assert by_number["id"] == VERSION_ID

    conn_missing = _mock_conn(None)
    conn_missing.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    assert repo.get_active_version(conn_missing) is None
    assert repo.get_version_by_number(conn_missing, 99) is None


@pytest.mark.unit
@pytest.mark.integration
def test_icp_scoring_repository_rules_and_versions() -> None:
    repo = PostgresIcpScoringRepository()
    rule_row = {
        "id": "vertical_fit",
        "version_id": VERSION_ID,
        "dimension": "vertical",
        "label": "Target vertical",
        "weight": 1.0,
        "threshold": {"categories": ["fintech"]},
        "enabled": True,
        "accept_hypothesis": False,
        "sort_order": 1,
    }
    version_row = {
        "id": VERSION_ID,
        "version_number": 2,
        "label": "ICP rules v2",
        "is_active": True,
        "created_at": None,
        "created_by": "operator",
    }

    conn_rules = _mock_conn([rule_row])
    rules = repo.list_rules_for_version(conn_rules, VERSION_ID)
    assert len(rules) == 1
    rules_sql = str(conn_rules.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "icp_scoring_rules" in rules_sql
    assert "ORDER BY sort_order ASC" in rules_sql

    conn_create = _mock_conn(version_row)
    created = repo.create_version(
        conn_create,
        version_number=2,
        label="ICP rules v2",
        created_by="operator",
        activate=True,
    )
    assert created["version_number"] == 2
    create_sql = str(conn_create.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO icp_scoring_versions" in create_sql

    conn_deactivate = _mock_conn(None)
    repo.deactivate_all_versions(conn_deactivate)
    deactivate_sql = str(
        conn_deactivate.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    )
    assert "UPDATE icp_scoring_versions SET is_active = FALSE" in deactivate_sql

    conn_insert_rule = _mock_conn(rule_row)
    inserted = repo.insert_rule(
        conn_insert_rule,
        version_id=VERSION_ID,
        rule_id="vertical_fit",
        dimension="vertical",
        label="Target vertical",
        weight=1.0,
        threshold={"categories": ["fintech"]},
        enabled=True,
        accept_hypothesis=False,
        sort_order=1,
    )
    assert inserted["id"] == "vertical_fit"
    insert_sql = str(conn_insert_rule.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO icp_scoring_rules" in insert_sql


@pytest.mark.unit
@pytest.mark.integration
def test_icp_scoring_repository_snapshots() -> None:
    from datetime import datetime, timezone

    repo = PostgresIcpScoringRepository()
    calculated_at = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    snapshot_row = {
        "id": UUID("88888888-8888-8888-8888-888888888881"),
        "company_id": COMPANY_ID,
        "version_id": VERSION_ID,
        "version_number": 1,
        "total_score": 7.0,
        "computed_score": 6.5,
        "breakdown": [],
        "missing_inputs": [],
        "calculated_at": calculated_at,
        "is_override": False,
        "override_reason": None,
        "override_by": None,
    }
    list_row = {**snapshot_row, "company_name": "Acme"}

    conn_insert = _mock_conn(snapshot_row)
    inserted = repo.insert_snapshot(
        conn_insert,
        company_id=COMPANY_ID,
        version_id=VERSION_ID,
        version_number=1,
        total_score=7.0,
        computed_score=6.5,
        breakdown=[{"rule_id": "vertical_fit", "points_awarded": 1.0}],
        missing_inputs=["company.stage"],
        calculated_at=calculated_at,
    )
    assert inserted["total_score"] == 7.0
    insert_sql = str(conn_insert.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO company_icp_score_snapshots" in insert_sql

    conn_latest = _mock_conn(snapshot_row)
    latest = repo.get_latest_snapshot_for_company(conn_latest, COMPANY_ID)
    assert latest is not None
    latest_sql = str(conn_latest.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY calculated_at DESC" in latest_sql

    conn_list = _mock_conn([list_row])
    rows = repo.list_latest_snapshots(conn_list, limit=25)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Acme"
    list_sql = str(conn_list.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "DISTINCT ON (s.company_id)" in list_sql
    assert conn_list.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == (25,)
