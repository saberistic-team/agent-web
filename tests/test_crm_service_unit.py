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
    assert isinstance(service._repos.admin_users, PostgresAdminUserRepository)


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
        "admin_users": admin_repo or MagicMock(),
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
def test_create_contact_assigns_roles_and_duplicate_warnings() -> None:
    contact_repo = MagicMock()
    contact_repo.find_duplicates.return_value = {
        "profile_url": [{"name": "Existing"}],
        "email": [],
        "name_company": [],
    }
    contact_repo.create.return_value = {"id": CONTACT_ID, "name": "Pat"}
    contact_repo.set_buying_roles.return_value = ["founder", "investor"]
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    result = service.create_contact(
        conn,
        name="Pat",
        profile_url="https://linkedin.com/in/pat/",
        email="pat@example.com",
        buying_roles=["founder", "investor"],
    )

    assert result["buying_roles"] == ["founder", "investor"]
    assert result["duplicate_warnings"]
    contact_repo.set_buying_roles.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_update_contact_returns_none_when_missing() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = None
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    assert service.update_contact(conn, CONTACT_ID, name="Pat") is None


@pytest.mark.unit
def test_archive_and_restore_contact() -> None:
    contact_repo = MagicMock()
    contact_repo.update.return_value = {"id": CONTACT_ID, "is_archived": True}
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    service.archive_contact(conn, CONTACT_ID)
    service.restore_contact(conn, CONTACT_ID)

    assert contact_repo.update.call_args_list[0].kwargs["is_archived"] is True
    assert contact_repo.update.call_args_list[1].kwargs["is_archived"] is False


@pytest.mark.unit
def test_get_contact_with_roles_and_search() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {"id": CONTACT_ID, "name": "Pat"}
    contact_repo.get_buying_roles.return_value = ["founder"]
    contact_repo.search.return_value = [{"id": CONTACT_ID, "name": "Pat"}]
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    contact = service.get_contact_with_roles(conn, CONTACT_ID)
    results = service.search_contacts(conn, query="pat")

    assert contact is not None
    assert contact["buying_roles"] == ["founder"]
    assert results[0]["buying_roles"] == ["founder"]


@pytest.mark.unit
def test_list_company_contacts_attaches_roles() -> None:
    contact_repo = MagicMock()
    contact_repo.list_for_company.return_value = [{"id": CONTACT_ID, "name": "Pat"}]
    contact_repo.get_buying_roles.return_value = ["technical_buyer"]
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    contacts = service.list_company_contacts(conn, COMPANY_ID)

    assert contacts[0]["buying_roles"] == ["technical_buyer"]
    contact_repo.list_for_company.assert_called_once_with(
        conn, COMPANY_ID, include_archived=False
    )


@pytest.mark.unit
def test_update_contact_persists_roles_and_warnings() -> None:
    contact_repo = MagicMock()
    contact_repo.get_by_id.return_value = {
        "id": CONTACT_ID,
        "name": "Pat",
        "company_id": COMPANY_ID,
        "profile_url": None,
        "email": None,
    }
    contact_repo.find_duplicates.return_value = {
        "profile_url": [],
        "email": [{"name": "Sam"}],
        "name_company": [],
    }
    contact_repo.update.return_value = {"id": CONTACT_ID, "name": "Patricia"}
    contact_repo.set_buying_roles.return_value = ["executive_buyer"]
    service, conn, _ = _service_with_mocks(contact_repo=contact_repo)

    result = service.update_contact(
        conn,
        CONTACT_ID,
        name="Patricia",
        buying_roles=["executive_buyer"],
    )

    assert result is not None
    assert result["buying_roles"] == ["executive_buyer"]
    assert any("Email" in warning for warning in result["duplicate_warnings"])
    contact_repo.set_buying_roles.assert_called_once()
