"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
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

    def record_company_with_contact(
        self,
        conn: psycopg.Connection,
        *,
        company_name: str,
        website: str | None,
        contact_email: str,
        contact_name: str | None = None,
    ) -> dict[str, Any]:
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
