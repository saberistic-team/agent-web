"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app.crm_uow import crm_transaction
from app.repositories import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)


@dataclass(frozen=True)
class CrmRepositories:
    companies: CompanyRepository
    contacts: ContactRepository
    source_records: SourceRecordRepository
    activities: ActivityRepository
    research_records: ResearchRecordRepository
    admin_users: AdminUserRepository


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        research_records=PostgresResearchRecordRepository(),
        admin_users=PostgresAdminUserRepository(),
    )


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
            activity = self._repos.activities.create(
                conn,
                activity_type=activity_type,
                summary=summary,
                company_id=company_id,
                contact_id=contact_id,
                metadata=metadata,
            )
        return activity

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
            record = self._repos.source_records.create(
                conn,
                source_type="project_brief",
                external_id=str(brief_id),
                company_id=company_id,
                contact_id=contact_id,
                payload=payload,
            )
        return record

    def get_admin_user_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
    ) -> dict[str, Any] | None:
        return self._repos.admin_users.get_by_email(conn, email)

    def list_companies(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.companies.list_all(conn, limit=limit)

    def get_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.companies.get_by_id(conn, company_id)

    def list_contacts_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.contacts.list_for_company(conn, company_id, limit=limit)

    def get_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.contacts.get_by_id(conn, contact_id)

    def list_research_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.research_records.list_for_company(
            conn,
            company_id,
            limit=limit,
        )

    def list_research_for_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._repos.research_records.list_for_contact(
            conn,
            contact_id,
            limit=limit,
        )

    def attach_research_record(
        self,
        conn: psycopg.Connection,
        *,
        record_type: str,
        company_id: UUID,
        body: str,
        contact_id: UUID | None = None,
        source_name: str | None = None,
        source_url: str | None = None,
        observed_value: str | None = None,
        observed_at: Any | None = None,
        confidence: float | None = None,
        review_at: Any | None = None,
        expires_at: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a research record without overwriting prior observations."""
        with crm_transaction(conn):
            record = self._repos.research_records.create(
                conn,
                record_type=record_type,
                company_id=company_id,
                body=body,
                contact_id=contact_id,
                source_name=source_name,
                source_url=source_url,
                observed_value=observed_value,
                observed_at=observed_at,
                confidence=confidence,
                review_at=review_at,
                expires_at=expires_at,
                metadata=metadata,
            )
        return record
