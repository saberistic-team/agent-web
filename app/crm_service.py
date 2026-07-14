"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.contacts import (
    ContactCreate,
    ContactUpdate,
    DuplicateWarning,
    find_duplicate_warnings,
    normalize_email,
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
                full_name=contact_name or contact_email,
                email=normalize_email(contact_email),
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

    def get_contact_with_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        contact = self._repos.contacts.get_by_id(conn, contact_id)
        if contact is None:
            return None
        roles = self._repos.contacts.get_buying_roles(conn, contact_id)
        return {**contact, "buying_roles": roles}

    def list_contacts(
        self,
        conn: psycopg.Connection,
        *,
        query: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, total = self._repos.contacts.list_page(
            conn,
            query=query,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        enriched: list[dict[str, Any]] = []
        for row in rows:
            contact_id = UUID(str(row["id"]))
            roles = self._repos.contacts.get_buying_roles(conn, contact_id)
            enriched.append({**row, "buying_roles": roles})
        return enriched, total

    def list_contacts_for_company_with_roles(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        contacts = self._repos.contacts.list_for_company(
            conn,
            company_id,
            limit=limit,
            include_archived=include_archived,
        )
        enriched: list[dict[str, Any]] = []
        for contact in contacts:
            contact_id = UUID(str(contact["id"]))
            roles = self._repos.contacts.get_buying_roles(conn, contact_id)
            enriched.append({**contact, "buying_roles": roles})
        return enriched

    def find_contact_duplicates(
        self,
        conn: psycopg.Connection,
        *,
        profile_url: str | None,
        email: str | None,
        full_name: str | None,
        company_id: UUID | None,
        exclude_contact_id: UUID | None = None,
    ) -> list[DuplicateWarning]:
        candidates = self._repos.contacts.list_all_active(conn)
        return find_duplicate_warnings(
            candidates,
            profile_url=profile_url,
            email=email,
            full_name=full_name,
            company_id=company_id,
            exclude_contact_id=exclude_contact_id,
        )

    def create_contact(
        self,
        conn: psycopg.Connection,
        payload: ContactCreate,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            contact = self._repos.contacts.create(
                conn,
                full_name=payload.full_name,
                company_id=payload.company_id,
                email=payload.email,
                title=payload.title,
                profile_url=payload.profile_url,
                email_provenance=payload.email_provenance,
                email_permission=payload.email_permission,
                last_interaction_at=payload.last_interaction_at,
                relationship_strength=payload.relationship_strength,
                notes=payload.notes,
            )
            roles = self._repos.contacts.set_buying_roles(
                conn,
                UUID(str(contact["id"])),
                payload.buying_roles,
            )
        return {**contact, "buying_roles": roles}

    def update_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        payload: ContactUpdate,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            contact = self._repos.contacts.update(
                conn,
                contact_id,
                full_name=payload.full_name,
                company_id=payload.company_id,
                clear_company=payload.clear_company,
                email=payload.email,
                clear_email=payload.clear_email,
                title=payload.title,
                clear_title=payload.clear_title,
                profile_url=payload.profile_url,
                clear_profile_url=payload.clear_profile_url,
                email_provenance=payload.email_provenance,
                clear_email_provenance=payload.clear_email_provenance,
                email_permission=payload.email_permission,
                clear_email_permission=payload.clear_email_permission,
                last_interaction_at=payload.last_interaction_at,
                clear_last_interaction=payload.clear_last_interaction,
                relationship_strength=payload.relationship_strength,
                clear_relationship_strength=payload.clear_relationship_strength,
                notes=payload.notes,
                clear_notes=payload.clear_notes,
            )
            roles = self._repos.contacts.get_buying_roles(conn, contact_id)
            if payload.buying_roles is not None:
                roles = self._repos.contacts.set_buying_roles(
                    conn,
                    contact_id,
                    payload.buying_roles,
                )
        if contact is None:
            return None
        return {**contact, "buying_roles": roles}

    def archive_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            contact = self._repos.contacts.update(
                conn,
                contact_id,
                status="archived",
            )
        if contact is None:
            return None
        roles = self._repos.contacts.get_buying_roles(conn, contact_id)
        return {**contact, "buying_roles": roles}

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
        audit_service.record_export_request(
            conn,
            actor_context=actor_context,
            export_type=export_type,
            filters=filters,
        )
        return {"export_type": export_type, "filters": filters or {}}

