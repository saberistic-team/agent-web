"""Unit tests for CRM service boundary."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.crm_service import CrmRepositories, CrmService
from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
)


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_records_company_contact_and_activity() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    activity_repo = MagicMock()
    source_repo = MagicMock()
    admin_repo = MagicMock()

    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.return_value = {"id": CONTACT_ID, "email": "lead@example.com"}
    activity_repo.create.return_value = {"id": "act-1", "summary": "Follow up"}

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=contact_repo,
            source_records=source_repo,
            activities=activity_repo,
            admin_users=admin_repo,
        )
    )
    conn = MagicMock()

    bundle = service.record_company_with_contact(
        conn,
        company_name="Acme",
        website="https://acme.dev",
        contact_email="lead@example.com",
    )
    assert bundle["company"]["name"] == "Acme"
    contact_repo.create.assert_called_once()

    activity = service.record_activity_for_company(
        conn,
        company_id=COMPANY_ID,
        activity_type="note",
        summary="Follow up",
        contact_id=CONTACT_ID,
    )
    assert activity["summary"] == "Follow up"


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_create_contact_assigns_roles_and_warnings() -> None:
    contact_repo = MagicMock()
    contact_repo.find_duplicates.return_value = {
        "profile_url": [{"name": "Existing"}],
        "email": [],
        "name_company": [],
    }
    contact_repo.create.return_value = {"id": CONTACT_ID, "name": "Pat"}
    contact_repo.set_buying_roles.return_value = ["founder"]

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    contact = service.create_contact(
        conn,
        name="Pat",
        profile_url="https://www.linkedin.com/in/pat/",
        buying_roles=["founder", "founder"],
    )
    assert contact["buying_roles"] == ["founder"]
    assert contact["duplicate_warnings"]
    contact_repo.set_buying_roles.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_archive_and_restore_contact() -> None:
    contact_repo = MagicMock()
    contact_repo.update.return_value = {"id": CONTACT_ID, "is_archived": True}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    archived = service.archive_contact(conn, CONTACT_ID)
    assert archived is not None
    contact_repo.update.assert_called_with(conn, CONTACT_ID, is_archived=True)

    contact_repo.update.return_value = {"id": CONTACT_ID, "is_archived": False}
    restored = service.restore_contact(conn, CONTACT_ID)
    assert restored is not None
    contact_repo.update.assert_called_with(conn, CONTACT_ID, is_archived=False)


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_links_project_brief_source() -> None:
    source_repo = MagicMock()
    source_repo.create.return_value = {
        "source_type": "project_brief",
        "external_id": "7",
    }
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    record = service.link_project_brief_source(
        conn,
        brief_id=7,
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
        payload={"status": "paid"},
    )
    assert record["external_id"] == "7"
    source_repo.create.assert_called_once_with(
        conn,
        source_type="project_brief",
        external_id="7",
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
        payload={"status": "paid"},
    )


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_update_contact_and_search() -> None:
    contact_repo = MagicMock()
    existing = {
        "id": CONTACT_ID,
        "name": "Pat",
        "company_id": COMPANY_ID,
        "profile_url": None,
        "email": None,
    }
    contact_repo.get_by_id.return_value = existing
    contact_repo.find_duplicates.return_value = {"profile_url": [], "email": [], "name_company": []}
    contact_repo.update.return_value = {**existing, "name": "Patricia"}
    contact_repo.set_buying_roles.return_value = ["executive_buyer"]
    contact_repo.get_buying_roles.return_value = ["executive_buyer"]
    contact_repo.search.return_value = [{"id": CONTACT_ID, "name": "Patricia"}]

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    updated = service.update_contact(
        conn,
        CONTACT_ID,
        name="Patricia",
        buying_roles=["executive_buyer"],
    )
    assert updated is not None
    assert updated["buying_roles"] == ["executive_buyer"]

    rows = service.search_contacts(conn, query="pat")
    assert len(rows) == 1
    assert rows[0]["buying_roles"] == ["executive_buyer"]


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_get_contact_and_list_company_contacts() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {"id": CONTACT_ID, "name": "Pat"}
    contact_repo.get_buying_roles.return_value = ["founder"]
    contact_repo.list_for_company.return_value = [{"id": CONTACT_ID, "name": "Pat"}]

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    contact = service.get_contact_with_roles(conn, CONTACT_ID)
    assert contact is not None
    assert contact["buying_roles"] == ["founder"]

    listed = service.list_company_contacts(conn, COMPANY_ID)
    assert listed[0]["buying_roles"] == ["founder"]


@pytest.mark.unit
@pytest.mark.integration
def test_crm_service_update_contact_missing_returns_none() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = None
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    assert service.update_contact(MagicMock(), CONTACT_ID, name="X") is None


@pytest.mark.unit
@pytest.mark.integration
def test_default_crm_repositories_use_postgres_backends() -> None:
    service = CrmService()
    assert isinstance(service._repos.companies, PostgresCompanyRepository)
    assert isinstance(service._repos.contacts, PostgresContactRepository)
    assert isinstance(service._repos.source_records, PostgresSourceRecordRepository)
    assert isinstance(service._repos.activities, PostgresActivityRepository)
    assert isinstance(service._repos.admin_users, PostgresAdminUserRepository)
