"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generator
from uuid import UUID

import psycopg

from app.repositories import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
    SourceRecordRepository,
)


@dataclass(frozen=True)
class CrmRepositories:
    companies: CompanyRepository
    contacts: ContactRepository
    source_records: SourceRecordRepository
    activities: ActivityRepository
    admin_users: AdminUserRepository


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        admin_users=PostgresAdminUserRepository(),
    )


@contextmanager
def crm_transaction(conn: psycopg.Connection) -> Generator[None, None, None]:
    """Commit once after all repository mutations succeed; rollback on any failure."""
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class CrmService:
    """Thin service layer for admin/import/discovery callers."""

    def __init__(self, repos: CrmRepositories | None = None) -> None:
        self._repos = repos or default_crm_repositories()

    def record_company_with_contact(
        self,
        conn: psycopg.Connection,
        *,
        company_name: str,
        website: str | None,
        contact_email: str,
        contact_name: str | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            company = self._repos.companies.create(
                conn,
                name=company_name,
                website=website,
            )
            contact = self._repos.contacts.create(
                conn,
                email=contact_email,
                full_name=contact_name,
                company_id=UUID(str(company["id"])),
            )
            return {"company": company, "contact": contact}

    def record_activity_for_company(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        activity_type: str,
        summary: str,
        contact_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            return self._repos.activities.create(
                conn,
                activity_type=activity_type,
                summary=summary,
                company_id=company_id,
                contact_id=contact_id,
                metadata=metadata,
            )

    def link_project_brief_source(
        self,
        conn: psycopg.Connection,
        *,
        brief_id: int,
        company_id: UUID,
        contact_id: UUID,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            return self._repos.source_records.create(
                conn,
                source_type="project_brief",
                external_id=str(brief_id),
                company_id=company_id,
                contact_id=contact_id,
                payload=payload,
            )

    def get_admin_user_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
    ) -> dict[str, Any] | None:
        return self._repos.admin_users.get_by_email(conn, email)
