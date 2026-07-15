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

    conn2 = _mock_conn(row)
    assert repo.get_by_email(conn2, "lead@example.com")["id"] == CONTACT_ID
    by_email_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "archived_at IS NULL" in by_email_sql

    conn3 = _mock_conn(row)
    assert repo.get_active_by_email(conn3, "lead@example.com")["id"] == CONTACT_ID
    active_sql = str(conn3.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "archived_at IS NULL" in active_sql

    archived_row = {**row, "archived_at": "2026-01-01", "id": UUID("99999999-9999-9999-9999-999999999999")}
    conn4 = _mock_conn(archived_row)
    assert repo.get_archived_by_email(conn4, "lead@example.com")["id"] == archived_row["id"]
    archived_sql = str(conn4.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "archived_at IS NOT NULL" in archived_sql

    conn5 = _mock_conn([row])
    contacts = repo.list_for_company(conn5, COMPANY_ID, limit=10)
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
