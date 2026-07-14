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
@pytest.mark.integration
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
    insert_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO companies" in insert_sql

    conn2 = _mock_conn(created)
    assert repo.get_by_id(conn2, COMPANY_ID)["id"] == COMPANY_ID

    updated = {**created, "status": "active"}
    conn3 = _mock_conn(updated)
    row = repo.update(conn3, COMPANY_ID, status="active")
    assert row is not None
    update_sql = str(conn3.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE companies" in update_sql
    assert "updated_at" in update_sql


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_create_search_and_roles() -> None:
    repo = PostgresContactRepository()
    row = {
        "id": CONTACT_ID,
        "name": "Lead",
        "email": "lead@example.com",
        "company_id": COMPANY_ID,
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        name="Lead",
        email="lead@example.com",
        normalized_email="lead@example.com",
        company_id=COMPANY_ID,
    )
    assert created["name"] == "Lead"
    insert_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO contacts" in insert_sql

    conn2 = _mock_conn([row])
    results = repo.search(conn2, query="lead")
    assert len(results) == 1

    conn3 = _mock_conn()
    conn3.cursor.return_value.__enter__.return_value.fetchall.side_effect = [
        [{"role": "founder"}, {"role": "investor"}],
    ]
    roles = repo.set_buying_roles(conn3, CONTACT_ID, ["founder", "investor"])
    assert roles == ["founder", "investor"]


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_find_duplicates() -> None:
    repo = PostgresContactRepository()
    duplicate = {"id": CONTACT_ID, "name": "Lead"}
    conn = _mock_conn()
    conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = [
        [duplicate],
        [],
        [],
    ]

    matches = repo.find_duplicates(
        conn,
        normalized_profile_url="linkedin.com/in/lead",
        normalized_email="lead@example.com",
        normalized_name="lead",
        company_id=COMPANY_ID,
    )
    assert len(matches["profile_url"]) == 1
    assert matches["email"] == []
    assert matches["name_company"] == []


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_update_archive_and_list_for_company() -> None:
    repo = PostgresContactRepository()
    row = {"id": CONTACT_ID, "name": "Lead", "is_archived": True}
    conn = _mock_conn(row)

    updated = repo.update(conn, CONTACT_ID, is_archived=True)
    assert updated is not None
    update_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "is_archived" in update_sql

    conn2 = _mock_conn([row])
    listed = repo.list_for_company(conn2, COMPANY_ID, include_archived=True)
    assert len(listed) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_find_duplicates_name_company() -> None:
    repo = PostgresContactRepository()
    duplicate = {"id": CONTACT_ID, "name": "lead"}
    conn = _mock_conn()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [duplicate]

    matches = repo.find_duplicates(
        conn,
        normalized_name="lead",
        company_id=COMPANY_ID,
        exclude_contact_id=UUID("99999999-9999-9999-9999-999999999999"),
    )
    assert len(matches["name_company"]) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_update_multiple_fields() -> None:
    repo = PostgresContactRepository()
    row = {
        "id": CONTACT_ID,
        "name": "Lead",
        "title": "CTO",
        "email": "lead@example.com",
    }
    conn = _mock_conn(row)

    updated = repo.update(
        conn,
        CONTACT_ID,
        name="Leader",
        title="CEO",
        email="ceo@example.com",
        notes="met at conf",
    )
    assert updated is not None
    update_sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "title = %s" in update_sql
    assert "notes = %s" in update_sql


@pytest.mark.unit
@pytest.mark.integration
def test_contact_repository_search_without_query_excludes_archived() -> None:
    repo = PostgresContactRepository()
    conn = _mock_conn([{"id": CONTACT_ID, "name": "Lead"}])
    results = repo.search(conn, query="", include_archived=False)
    assert len(results) == 1
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "is_archived = FALSE" in sql


@pytest.mark.unit
@pytest.mark.integration
def test_company_repository_list_all() -> None:
    repo = PostgresCompanyRepository()
    conn = _mock_conn([{"id": COMPANY_ID, "name": "Acme"}])
    companies = repo.list_all(conn)
    assert companies[0]["name"] == "Acme"


@pytest.mark.unit
@pytest.mark.integration
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

    conn2 = _mock_conn(row)
    found = repo.get_by_source(conn2, source_type="project_brief", external_id="42")
    assert found is not None
    lookup_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "source_type" in lookup_sql and "external_id" in lookup_sql


@pytest.mark.unit
@pytest.mark.integration
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

    conn2 = _mock_conn([row])
    activities = repo.list_for_company(conn2, COMPANY_ID, limit=10)
    assert len(activities) == 1
    list_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY created_at DESC" in list_sql


@pytest.mark.unit
@pytest.mark.integration
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

    conn2 = _mock_conn(row)
    assert repo.get_by_email(conn2, "admin@saberistic.com")["id"] == ADMIN_ID

    conn3 = _mock_conn(row)
    assert repo.get_by_id(conn3, ADMIN_ID)["email"] == "admin@saberistic.com"
