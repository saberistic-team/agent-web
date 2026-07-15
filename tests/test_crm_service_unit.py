"""Unit tests for CRM service boundary."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.crm_service import CrmRepositories, CrmService
from app.companies import CompanyCreate, CompanyUpdate
from app.contacts import ContactCreate, ContactUpdate
from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresPipelineRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
)


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_crm_service_records_company_contact_and_activity() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    activity_repo = MagicMock()
    source_repo = MagicMock()
    research_repo = MagicMock()
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
            research_records=research_repo,
            admin_users=admin_repo,
            pipeline=MagicMock(),
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
    conn.commit.assert_called()

    activity = service.record_activity_for_company(
        conn,
        company_id=COMPANY_ID,
        activity_type="note",
        summary="Follow up",
        contact_id=CONTACT_ID,
    )
    assert activity["summary"] == "Follow up"


@pytest.mark.unit
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
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=MagicMock(),
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
    conn.commit.assert_called_once()
    source_repo.create.assert_called_once_with(
        conn,
        source_type="project_brief",
        external_id="7",
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
        payload={"status": "paid"},
    )


@pytest.mark.unit
def test_default_crm_repositories_use_postgres_backends() -> None:
    service = CrmService()
    assert isinstance(service._repos.companies, PostgresCompanyRepository)
    assert isinstance(service._repos.contacts, PostgresContactRepository)
    assert isinstance(service._repos.source_records, PostgresSourceRecordRepository)
    assert isinstance(service._repos.activities, PostgresActivityRepository)
    assert isinstance(service._repos.research_records, PostgresResearchRecordRepository)
    assert isinstance(service._repos.admin_users, PostgresAdminUserRepository)
    assert isinstance(service._repos.pipeline, PostgresPipelineRepository)


def _service_with_mocks(
    *,
    company_repo: MagicMock | None = None,
    contact_repo: MagicMock | None = None,
    activity_repo: MagicMock | None = None,
    source_repo: MagicMock | None = None,
    admin_repo: MagicMock | None = None,
) -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    repos = {
        "companies": company_repo or MagicMock(),
        "contacts": contact_repo or MagicMock(),
        "source_records": source_repo or MagicMock(),
        "activities": activity_repo or MagicMock(),
        "research_records": MagicMock(),
        "admin_users": admin_repo or MagicMock(),
        "pipeline": MagicMock(),
    }
    service = CrmService(repos=CrmRepositories(**repos))
    conn = MagicMock()
    return service, conn, repos


@pytest.mark.unit
def test_record_company_with_contact_commits_once_on_success() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.return_value = {"id": CONTACT_ID, "email": "lead@example.com"}
    service, conn, _ = _service_with_mocks(
        company_repo=company_repo,
        contact_repo=contact_repo,
    )

    service.record_company_with_contact(
        conn,
        company_name="Acme",
        website="https://acme.dev",
        contact_email="lead@example.com",
    )

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_record_company_with_contact_rolls_back_when_contact_create_fails() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.side_effect = RuntimeError("duplicate email")
    service, conn, _ = _service_with_mocks(
        company_repo=company_repo,
        contact_repo=contact_repo,
    )

    with pytest.raises(RuntimeError, match="duplicate email"):
        service.record_company_with_contact(
            conn,
            company_name="Acme",
            website="https://acme.dev",
            contact_email="lead@example.com",
        )

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_record_company_with_contact_retry_after_rollback() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.side_effect = [
        RuntimeError("duplicate email"),
        {"id": CONTACT_ID, "email": "lead@example.com"},
    ]
    service, conn, _ = _service_with_mocks(
        company_repo=company_repo,
        contact_repo=contact_repo,
    )

    with pytest.raises(RuntimeError, match="duplicate email"):
        service.record_company_with_contact(
            conn,
            company_name="Acme",
            website=None,
            contact_email="lead@example.com",
        )

    bundle = service.record_company_with_contact(
        conn,
        company_name="Acme",
        website=None,
        contact_email="lead@example.com",
    )

    assert bundle["contact"]["email"] == "lead@example.com"
    assert conn.rollback.call_count == 1
    assert conn.commit.call_count == 1


@pytest.mark.unit
def test_single_record_writes_commit_once() -> None:
    activity_repo = MagicMock()
    source_repo = MagicMock()
    activity_repo.create.return_value = {"id": "act-1", "summary": "Follow up"}
    source_repo.create.return_value = {"source_type": "project_brief", "external_id": "7"}
    service, conn, _ = _service_with_mocks(
        activity_repo=activity_repo,
        source_repo=source_repo,
    )

    service.record_activity_for_company(
        conn,
        company_id=COMPANY_ID,
        activity_type="note",
        summary="Follow up",
    )
    service.link_project_brief_source(
        conn,
        brief_id=7,
        company_id=COMPANY_ID,
        contact_id=CONTACT_ID,
    )

    assert conn.commit.call_count == 2
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_read_methods_do_not_change_transaction_state() -> None:
    admin_repo = MagicMock()
    admin_repo.get_by_email.return_value = {"email": "admin@saberistic.com"}
    service, conn, _ = _service_with_mocks(admin_repo=admin_repo)

    user = service.get_admin_user_by_email(conn, "admin@saberistic.com")

    assert user is not None
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_crm_service_research_record_helpers() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.list_all.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    company_repo.get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.list_for_company.return_value = [{"id": CONTACT_ID, "email": "lead@example.com"}]
    contact_repo.get_by_id.return_value = {"id": CONTACT_ID, "email": "lead@example.com"}

    service, conn, repos = _service_with_mocks(
        company_repo=company_repo,
        contact_repo=contact_repo,
    )
    research_repo = repos["research_records"]
    research_repo.list_for_company.return_value = [{"record_type": "hypothesis"}]
    research_repo.list_for_contact.return_value = [{"record_type": "verified_fact"}]
    research_repo.create.return_value = {"id": "rec-1", "body": "Series B"}

    assert len(service.list_companies(conn)) == 1
    assert service.get_company(conn, COMPANY_ID)["name"] == "Acme"
    assert len(service.list_contacts_for_company(conn, COMPANY_ID)) == 1
    assert service.get_contact(conn, CONTACT_ID)["email"] == "lead@example.com"
    assert len(service.list_research_for_company(conn, COMPANY_ID)) == 1
    assert len(service.list_research_for_contact(conn, CONTACT_ID)) == 1

    record = service.attach_research_record(
        conn,
        record_type="hypothesis",
        company_id=COMPANY_ID,
        body="Likely buyer",
    )
    assert record["body"] == "Series B"
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_company_crud_helpers_commit_and_return_nonblocking_domain_warnings() -> None:
    company_repo = MagicMock()
    company_repo.find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Existing", "domain": "acme.dev"}]
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme", "domain": "acme.dev"}
    company_repo.update.return_value = {"id": COMPANY_ID, "name": "Acme Updated", "domain": "acme.dev"}
    company_repo.archive.return_value = {"id": COMPANY_ID, "archived_at": "now"}
    company_repo.restore.return_value = {"id": COMPANY_ID, "archived_at": None}
    service, conn, _ = _service_with_mocks(company_repo=company_repo)

    created = service.create_company(conn, company=CompanyCreate(name="Acme", domain="www.acme.dev"))
    assert created["company"]["name"] == "Acme"
    assert len(created["duplicate_warnings"]) == 1
    updated = service.update_company(
        conn, COMPANY_ID, company=CompanyUpdate(name="Acme Updated", domain="acme.dev")
    )
    assert updated is not None and updated["company"]["name"] == "Acme Updated"
    assert service.archive_company(conn, COMPANY_ID)["archived_at"] == "now"
    assert service.restore_company(conn, COMPANY_ID)["archived_at"] is None
    assert conn.commit.call_count == 4


@pytest.mark.unit
def test_contact_crud_helpers_commit_and_return_nonblocking_duplicate_warnings() -> None:
    contact_repo = MagicMock()
    contact_repo.find_by_profile_url.return_value = [
        {"id": CONTACT_ID, "full_name": "Ada", "profile_url": "https://linkedin.com/in/ada"}
    ]
    contact_repo.get_by_email.return_value = {"id": CONTACT_ID, "full_name": "Ada", "email": "ada@example.com"}
    contact_repo.find_by_name_company.return_value = [
        {"id": CONTACT_ID, "full_name": "Ada", "company_id": COMPANY_ID}
    ]
    contact_repo.create.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "buying_roles": ["founder", "technical_buyer"],
    }
    contact_repo.update.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Updated",
        "buying_roles": ["executive_buyer"],
    }
    contact_repo.archive.return_value = {"id": CONTACT_ID, "archived_at": "now"}
    contact_repo.restore.return_value = {"id": CONTACT_ID, "archived_at": None}
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    created = service.create_contact(
        conn,
        contact=ContactCreate(
            full_name="Ada",
            profile_url="https://www.linkedin.com/in/ada",
            email="ada@example.com",
            company_id=COMPANY_ID,
            buying_roles=["founder"],
        ),
    )
    assert created["contact"]["full_name"] == "Ada"
    assert len(created["duplicate_warnings"]) == 3

    updated = service.update_contact(
        conn,
        CONTACT_ID,
        contact=ContactUpdate(
            full_name="Ada Updated",
            profile_url="https://linkedin.com/in/ada",
            email="ada@example.com",
            company_id=COMPANY_ID,
            buying_roles=["executive_buyer"],
        ),
    )
    assert updated is not None and updated["contact"]["full_name"] == "Ada Updated"
    assert service.archive_contact(conn, CONTACT_ID)["archived_at"] == "now"
    assert service.restore_contact(conn, CONTACT_ID)["archived_at"] is None
    assert conn.commit.call_count == 4


@pytest.mark.unit
def test_search_contacts_aliases_list_contacts() -> None:
    from unittest.mock import patch

    service, conn, _ = _service_with_mocks()
    with patch.object(service, "list_contacts", return_value=[{"id": "x"}]) as listed:
        assert service.search_contacts(conn, query="pat") == [{"id": "x"}]
        listed.assert_called_once_with(conn, query="pat")
