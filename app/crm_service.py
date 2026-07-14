"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.contacts import ContactDuplicateMatch, ContactFormData, contact_display_name
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


@dataclass(frozen=True)
class ContactListFilters:
    page: int = 1
    per_page: int = 50
    query: str | None = None
    company_id: UUID | None = None
    include_archived: bool = False


def normalize_contact_filters(
    *,
    page: int = 1,
    per_page: int = 50,
    query: str | None = None,
    company_id: str | None = None,
    include_archived: bool = False,
) -> ContactListFilters:
    safe_page = max(page, 1)
    safe_per_page = max(1, min(per_page, 100))
    normalized_query = query.strip() if query and query.strip() else None
    parsed_company_id: UUID | None = None
    if company_id and company_id.strip():
        parsed_company_id = UUID(company_id.strip())
    return ContactListFilters(
        page=safe_page,
        per_page=safe_per_page,
        query=normalized_query,
        company_id=parsed_company_id,
        include_archived=include_archived,
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
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        contacts = self._repos.contacts.list_for_company(
            conn,
            company_id,
            limit=limit,
            include_archived=include_archived,
        )
        return self._attach_buying_roles(conn, contacts)

    def get_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        contact = self._repos.contacts.get_by_id(conn, contact_id)
        if contact is None:
            return None
        roles = self._repos.contacts.list_buying_roles(conn, contact_id)
        enriched = dict(contact)
        enriched["buying_roles"] = roles
        return enriched

    def list_contacts(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
        query: str | None = None,
        company_id: UUID | None = None,
        include_archived: bool = False,
    ) -> tuple[list[dict[str, Any]], int, ContactListFilters]:
        filters = normalize_contact_filters(
            page=page,
            per_page=per_page,
            query=query,
            company_id=str(company_id) if company_id else None,
            include_archived=include_archived,
        )
        rows, total = self._repos.contacts.list_page(
            conn,
            page=filters.page,
            per_page=filters.per_page,
            query=filters.query,
            company_id=filters.company_id,
            include_archived=filters.include_archived,
        )
        return self._attach_buying_roles(conn, rows), total, filters

    def find_contact_duplicates(
        self,
        conn: psycopg.Connection,
        *,
        email: str | None = None,
        profile_url: str | None = None,
        full_name: str | None = None,
        company_id: UUID | None = None,
        exclude_contact_id: UUID | None = None,
    ) -> list[ContactDuplicateMatch]:
        rows = self._repos.contacts.find_possible_duplicates(
            conn,
            email=email,
            profile_url=profile_url,
            full_name=full_name,
            company_id=company_id,
            exclude_contact_id=exclude_contact_id,
        )
        reason_labels = {
            "email": "matching email",
            "profile_url": "matching profile URL",
            "name_company": "matching name and company",
        }
        matches: list[ContactDuplicateMatch] = []
        for row in rows:
            reason_key = str(row.get("duplicate_reason", "unknown"))
            matches.append(
                ContactDuplicateMatch(
                    contact_id=UUID(str(row["id"])),
                    reason=reason_labels.get(reason_key, reason_key),
                    contact=row,
                )
            )
        return matches

    def create_contact(
        self,
        conn: psycopg.Connection,
        *,
        payload: ContactFormData,
    ) -> dict[str, Any]:
        duplicates = self.find_contact_duplicates(
            conn,
            email=payload.email,
            profile_url=payload.profile_url,
            full_name=payload.full_name,
            company_id=payload.parsed_company_id(),
        )
        if duplicates and not payload.confirm_duplicates:
            raise ValueError("possible duplicate contacts detected")
        with crm_transaction(conn):
            contact = self._repos.contacts.create(
                conn,
                email=payload.email,
                full_name=payload.full_name,
                company_id=payload.parsed_company_id(),
                title=payload.title,
                profile_url=payload.profile_url,
                email_provenance=payload.email_provenance,
                email_permission=payload.email_permission,
                last_interaction_at=payload.parsed_last_interaction_at(),
                relationship_strength=payload.relationship_strength,
                notes=payload.notes,
            )
            contact_id = UUID(str(contact["id"]))
            roles = self._repos.contacts.set_buying_roles(
                conn,
                contact_id,
                payload.buying_roles,
            )
        enriched = dict(contact)
        enriched["buying_roles"] = roles
        return enriched

    def update_contact(
        self,
        conn: psycopg.Connection,
        *,
        contact_id: UUID,
        payload: ContactFormData,
    ) -> dict[str, Any]:
        existing = self._repos.contacts.get_by_id(conn, contact_id)
        if existing is None:
            raise ValueError("contact not found")
        duplicates = self.find_contact_duplicates(
            conn,
            email=payload.email,
            profile_url=payload.profile_url,
            full_name=payload.full_name,
            company_id=payload.parsed_company_id(),
            exclude_contact_id=contact_id,
        )
        if duplicates and not payload.confirm_duplicates:
            raise ValueError("possible duplicate contacts detected")
        with crm_transaction(conn):
            updated = self._repos.contacts.update(
                conn,
                contact_id,
                email=payload.email,
                full_name=payload.full_name,
                company_id=payload.parsed_company_id(),
                title=payload.title,
                profile_url=payload.profile_url,
                email_provenance=payload.email_provenance,
                email_permission=payload.email_permission,
                last_interaction_at=payload.parsed_last_interaction_at(),
                relationship_strength=payload.relationship_strength,
                notes=payload.notes,
                clear_company=not (payload.company_id and payload.company_id.strip()),
            )
            if updated is None:
                raise ValueError("contact not found")
            roles = self._repos.contacts.set_buying_roles(
                conn,
                contact_id,
                payload.buying_roles,
            )
        enriched = dict(updated)
        enriched["buying_roles"] = roles
        return enriched

    def archive_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        with crm_transaction(conn):
            archived = self._repos.contacts.archive(conn, contact_id)
        return archived

    def _attach_buying_roles(
        self,
        conn: psycopg.Connection,
        contacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not contacts:
            return []
        contact_ids = [UUID(str(contact["id"])) for contact in contacts]
        role_map = self._repos.contacts.list_buying_roles_for_contacts(conn, contact_ids)
        enriched: list[dict[str, Any]] = []
        for contact in contacts:
            contact_id = UUID(str(contact["id"]))
            item = dict(contact)
            item["buying_roles"] = role_map.get(contact_id, [])
            item["display_name"] = contact_display_name(item)
            enriched.append(item)
        return enriched

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

