"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from app import contacts as contacts_module
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


class CrmService:
    """Thin service layer for admin/import/discovery callers."""

    def __init__(self, repos: CrmRepositories | None = None) -> None:
        self._repos = repos or default_crm_repositories()

    def create_contact(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        email: str | None = None,
        email_permission: str | None = None,
        email_provenance: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_profile = contacts_module.normalize_profile_url(profile_url)
        normalized_email = contacts_module.normalize_email(email)
        normalized_name = contacts_module.normalize_name(name)
        duplicates = self._repos.contacts.find_duplicates(
            conn,
            normalized_profile_url=normalized_profile,
            normalized_email=normalized_email,
            normalized_name=normalized_name,
            company_id=company_id,
        )
        roles = contacts_module.parse_buying_roles(buying_roles or [])
        with crm_transaction(conn):
            contact = self._repos.contacts.create(
                conn,
                name=name.strip(),
                company_id=company_id,
                title=title.strip() if title else None,
                profile_url=profile_url.strip() if profile_url else None,
                normalized_profile_url=normalized_profile,
                email=email.strip() if email else None,
                normalized_email=normalized_email,
                email_permission=email_permission,
                email_provenance=email_provenance.strip() if email_provenance else None,
                last_interaction_at=last_interaction_at,
                relationship_strength=relationship_strength,
                notes=notes.strip() if notes else None,
            )
            if roles:
                self._repos.contacts.set_buying_roles(conn, UUID(str(contact["id"])), roles)
        contact["buying_roles"] = roles
        contact["duplicate_warnings"] = contacts_module.duplicate_warnings(matches=duplicates)
        return contact

    def update_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        name: str | None = None,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        email: str | None = None,
        email_permission: str | None = None,
        email_provenance: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
        is_archived: bool | None = None,
    ) -> dict[str, Any] | None:
        existing = self._repos.contacts.get_by_id(conn, contact_id)
        if existing is None:
            return None

        resolved_name = name.strip() if name is not None else existing.get("name")
        resolved_company_id = company_id if company_id is not None else existing.get("company_id")
        if resolved_company_id is not None and not isinstance(resolved_company_id, UUID):
            resolved_company_id = UUID(str(resolved_company_id))

        resolved_profile = profile_url if profile_url is not None else existing.get("profile_url")
        resolved_email = email if email is not None else existing.get("email")

        normalized_profile = contacts_module.normalize_profile_url(
            str(resolved_profile) if resolved_profile else None
        )
        normalized_email = contacts_module.normalize_email(
            str(resolved_email) if resolved_email else None
        )
        normalized_name = contacts_module.normalize_name(
            str(resolved_name) if resolved_name else None
        )
        duplicates = self._repos.contacts.find_duplicates(
            conn,
            normalized_profile_url=normalized_profile,
            normalized_email=normalized_email,
            normalized_name=normalized_name,
            company_id=resolved_company_id,
            exclude_contact_id=contact_id,
        )

        roles = (
            contacts_module.parse_buying_roles(buying_roles)
            if buying_roles is not None
            else self._repos.contacts.get_buying_roles(conn, contact_id)
        )
        with crm_transaction(conn):
            updated = self._repos.contacts.update(
                conn,
                contact_id,
                name=resolved_name.strip() if resolved_name else None,
                company_id=company_id,
                title=title.strip() if title is not None else None,
                profile_url=profile_url.strip() if profile_url is not None else None,
                normalized_profile_url=normalized_profile,
                email=email.strip() if email is not None else None,
                normalized_email=normalized_email,
                email_permission=email_permission,
                email_provenance=email_provenance.strip() if email_provenance is not None else None,
                last_interaction_at=last_interaction_at,
                relationship_strength=relationship_strength,
                notes=notes.strip() if notes is not None else None,
                is_archived=is_archived,
            )
            if updated is None:
                return None
            if buying_roles is not None:
                roles = self._repos.contacts.set_buying_roles(conn, contact_id, roles)
        updated["buying_roles"] = roles
        updated["duplicate_warnings"] = contacts_module.duplicate_warnings(matches=duplicates)
        return updated

    def archive_contact(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.update(conn, contact_id, is_archived=True)

    def restore_contact(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.update(conn, contact_id, is_archived=False)

    def get_contact_with_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        contact = self._repos.contacts.get_by_id(conn, contact_id)
        if contact is None:
            return None
        contact["buying_roles"] = self._repos.contacts.get_buying_roles(conn, contact_id)
        return contact

    def search_contacts(
        self,
        conn: psycopg.Connection,
        *,
        query: str = "",
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self._repos.contacts.search(
            conn,
            query=query,
            include_archived=include_archived,
        )
        for row in rows:
            row["buying_roles"] = self._repos.contacts.get_buying_roles(
                conn, UUID(str(row["id"]))
            )
        return rows

    def list_company_contacts(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self._repos.contacts.list_for_company(
            conn,
            company_id,
            include_archived=include_archived,
        )
        for row in rows:
            row["buying_roles"] = self._repos.contacts.get_buying_roles(
                conn, UUID(str(row["id"]))
            )
        return rows

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
                name=contact_name or contact_email,
                email=contact_email,
                normalized_email=contacts_module.normalize_email(contact_email),
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
