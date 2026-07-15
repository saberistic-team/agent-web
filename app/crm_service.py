"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.companies import CompanyCreate, CompanyUpdate, find_domain_duplicate_warnings
from app.contacts import (
    ContactCreate,
    ContactUpdate,
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
)
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
                full_name=contact_name or contact_email.split("@", 1)[0],
                email=contact_email,
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
        query: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        target_status: str | None = None,
        freshness: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.companies.list_all(
            conn,
            limit=limit,
            query=query,
            category=category,
            stage=stage,
            target_status=target_status,
            freshness=freshness,
            include_archived=include_archived,
        )

    def get_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.companies.get_by_id(conn, company_id)

    def create_company(
        self,
        conn: psycopg.Connection,
        *,
        company: CompanyCreate,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            duplicates = self._repos.companies.find_by_domain(conn, company.domain) if company.domain else []
            created = self._repos.companies.create(conn, **company.model_dump())
        return {
            "company": created,
            "duplicate_warnings": find_domain_duplicate_warnings(
                duplicates, domain=company.domain
            ),
        }

    def update_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        company: CompanyUpdate,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            duplicates = (
                self._repos.companies.find_by_domain(
                    conn, company.domain, exclude_company_id=company_id
                )
                if company.domain
                else []
            )
            updated = self._repos.companies.update(
                conn, company_id, **company.model_dump(exclude_none=True)
            )
        if updated is None:
            return None
        return {
            "company": updated,
            "duplicate_warnings": find_domain_duplicate_warnings(
                duplicates, domain=company.domain, exclude_company_id=company_id
            ),
        }

    def archive_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.companies.archive(conn, company_id)

    def restore_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.companies.restore(conn, company_id)

    def list_contacts_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.contacts.list_for_company(
            conn, company_id, limit=limit, include_archived=include_archived
        )

    def list_contacts(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
        query: str | None = None,
        company_id: UUID | None = None,
        buying_role: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self._repos.contacts.list_all(
            conn,
            limit=limit,
            query=query,
            company_id=company_id,
            buying_role=buying_role,
            include_archived=include_archived,
        )

    def get_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        return self._repos.contacts.get_by_id(conn, contact_id)

    def create_contact(
        self,
        conn: psycopg.Connection,
        *,
        contact: ContactCreate,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            profile_matches = (
                self._repos.contacts.find_by_profile_url(conn, contact.profile_url)
                if contact.profile_url
                else []
            )
            email_matches = (
                [existing]
                if contact.email and (existing := self._repos.contacts.get_by_email(conn, contact.email))
                else []
            )
            name_company_matches = (
                self._repos.contacts.find_by_name_company(
                    conn,
                    full_name=contact.full_name,
                    company_id=contact.company_id,
                )
                if contact.company_id
                else []
            )
            created = self._repos.contacts.create(conn, **contact.model_dump())
        duplicate_warnings = [
            *find_profile_url_duplicate_warnings(profile_matches, profile_url=contact.profile_url),
            *find_email_duplicate_warnings(email_matches, email=contact.email),
            *find_name_company_duplicate_warnings(
                name_company_matches,
                full_name=contact.full_name,
                company_id=contact.company_id,
            ),
        ]
        return {"contact": created, "duplicate_warnings": duplicate_warnings}

    def update_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        contact: ContactUpdate,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            profile_matches = (
                self._repos.contacts.find_by_profile_url(
                    conn, contact.profile_url, exclude_contact_id=contact_id
                )
                if contact.profile_url
                else []
            )
            email_matches: list[dict[str, Any]] = []
            if contact.email:
                existing = self._repos.contacts.get_by_email(conn, contact.email)
                if existing is not None and str(existing.get("id")) != str(contact_id):
                    email_matches.append(existing)
            name_company_matches = (
                self._repos.contacts.find_by_name_company(
                    conn,
                    full_name=contact.full_name,
                    company_id=contact.company_id,
                    exclude_contact_id=contact_id,
                )
                if contact.company_id
                else []
            )
            updated = self._repos.contacts.update(
                conn, contact_id, **contact.model_dump()
            )
        if updated is None:
            return None
        duplicate_warnings = [
            *find_profile_url_duplicate_warnings(
                profile_matches, profile_url=contact.profile_url, exclude_contact_id=contact_id
            ),
            *find_email_duplicate_warnings(
                email_matches, email=contact.email, exclude_contact_id=contact_id
            ),
            *find_name_company_duplicate_warnings(
                name_company_matches,
                full_name=contact.full_name,
                company_id=contact.company_id,
                exclude_contact_id=contact_id,
            ),
        ]
        return {"contact": updated, "duplicate_warnings": duplicate_warnings}

    def archive_contact(
        self, conn: psycopg.Connection, contact_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.archive(conn, contact_id)

    def restore_contact(
        self, conn: psycopg.Connection, contact_id: UUID
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            return self._repos.contacts.restore(conn, contact_id)

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

    def import_batch(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        batch_id: str,
        source_type: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import source records and append an audit event."""
        with crm_transaction(conn):
            created: list[dict[str, Any]] = []
            for index, record in enumerate(records):
                created.append(
                    self._repos.source_records.create(
                        conn,
                        source_type="import",
                        external_id=f"{batch_id}:{index}",
                        payload={"source_type": source_type, **record},
                    )
                )
            audit_service.record_import_batch(
                conn,
                actor_context=actor_context,
                batch_id=batch_id,
                source_type=source_type,
                record_count=len(created),
            )
        return {"batch_id": batch_id, "created": created, "record_count": len(created)}

    def delete_entity(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        entity_type: str,
        entity_id: str,
        summary_before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a destructive delete audit event (storage delete ships later)."""
        with crm_transaction(conn):
            audit_service.record_entity_delete(
                conn,
                actor_context=actor_context,
                entity_type=entity_type,
                entity_id=entity_id,
                summary_before=summary_before,
            )
        return {"entity_type": entity_type, "entity_id": entity_id, "deleted": True}

    def update_pipeline(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        entity_id: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_pipeline_update(
                conn,
                actor_context=actor_context,
                entity_id=entity_id,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"entity_id": entity_id, "summary_after": summary_after}

    def update_scoring_rule(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        rule_id: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_scoring_rule_update(
                conn,
                actor_context=actor_context,
                rule_id=rule_id,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"rule_id": rule_id, "summary_after": summary_after}

    def update_analytics_config(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        config_key: str,
        summary_before: dict[str, Any] | None,
        summary_after: dict[str, Any],
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_analytics_config_update(
                conn,
                actor_context=actor_context,
                config_key=config_key,
                summary_before=summary_before,
                summary_after=summary_after,
            )
        return {"config_key": config_key, "summary_after": summary_after}

    def request_export(
        self,
        conn: psycopg.Connection,
        *,
        actor_context: ActorContext,
        export_type: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            audit_service.record_export_request(
                conn,
                actor_context=actor_context,
                export_type=export_type,
                filters=filters,
            )
        return {"export_type": export_type, "filters": filters or {}}

