"""Unit tests for CRM transaction ownership (repositories vs service)."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.crm_service import CrmRepositories, CrmService
from app.crm_uow import crm_transaction
from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
@pytest.mark.parametrize(
    "repo_factory,create_kwargs",
    [
        (
            PostgresCompanyRepository,
            {"name": "Acme", "website": "https://acme.dev"},
        ),
        (
            PostgresContactRepository,
            {"full_name": "Lead", "email": "lead@example.com", "company_id": COMPANY_ID},
        ),
        (
            PostgresSourceRecordRepository,
            {
                "source_type": "manual",
                "external_id": "ext-1",
                "company_id": COMPANY_ID,
            },
        ),
        (
            PostgresActivityRepository,
            {
                "activity_type": "note",
                "summary": "Hello",
                "company_id": COMPANY_ID,
            },
        ),
        (
            PostgresAdminUserRepository,
            {"email": "admin@saberistic.com", "role": "admin"},
        ),
    ],
)
def test_repository_writes_do_not_commit(
    repo_factory: type,
    create_kwargs: dict,
) -> None:
    row = {"id": COMPANY_ID, **create_kwargs}
    conn = _mock_conn(row)
    repo = repo_factory()
    repo.create(conn, **create_kwargs)
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_commits_on_success() -> None:
    conn = MagicMock()
    with crm_transaction(conn):
        pass
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_rolls_back_on_failure() -> None:
    conn = MagicMock()
    with pytest.raises(RuntimeError, match="boom"):
        with crm_transaction(conn):
            raise RuntimeError("boom")
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_crm_transaction_retry_after_rollback() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError):
        with crm_transaction(conn):
            raise ValueError("first attempt")
    conn.rollback.assert_called_once()

    with crm_transaction(conn):
        pass
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_record_company_with_contact_commits_once_on_success() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.return_value = {"id": CONTACT_ID, "email": "lead@example.com"}

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
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
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_record_company_with_contact_rolls_back_when_contact_fails() -> None:
    company_repo = MagicMock()
    contact_repo = MagicMock()
    company_repo.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    contact_repo.create.side_effect = RuntimeError("duplicate email")

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=contact_repo,
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

    with pytest.raises(RuntimeError, match="duplicate email"):
        service.record_company_with_contact(
            conn,
            company_name="Acme",
            website="https://acme.dev",
            contact_email="lead@example.com",
        )

    company_repo.create.assert_called_once()
    contact_repo.create.assert_called_once()
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_single_record_service_operations_commit_once() -> None:
    activity_repo = MagicMock()
    source_repo = MagicMock()
    activity_repo.create.return_value = {"summary": "Follow up"}
    source_repo.create.return_value = {"external_id": "7"}

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=source_repo,
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    conn = MagicMock()

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
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=admin_repo,
        )
    )
    conn = MagicMock()

    user = service.get_admin_user_by_email(conn, "admin@saberistic.com")

    assert user is not None
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()
