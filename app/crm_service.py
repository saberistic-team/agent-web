"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

from app import audit_service
from app.actor_context import ActorContext
from app.crm_uow import crm_transaction
from app.pipeline import (
    ConfirmRequiredError,
    InvalidStageError,
    InvalidTransitionError,
    PipelineError,
    ReasonRequiredError,
    validate_activity_type,
    validate_stage,
    validate_transition,
)
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
    PostgresStageHistoryRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
    StageHistoryRepository,
)
from app.repositories.postgres import get_repositories


@dataclass(frozen=True)
class CrmRepositories:
    companies: CompanyRepository
    contacts: ContactRepository
    source_records: SourceRecordRepository
    activities: ActivityRepository
    stage_history: StageHistoryRepository
    research_records: ResearchRecordRepository
    admin_users: AdminUserRepository


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        stage_history=PostgresStageHistoryRepository(),
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
        owner: str | None = None,
        expected_value: float | None = None,
    ) -> dict[str, Any]:
        with crm_transaction(conn):
            company = self._repos.companies.create(
                conn,
                name=company_name,
                website=website,
                owner=owner,
                expected_value=expected_value,
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
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        validate_activity_type(activity_type)
        with crm_transaction(conn):
            activity = self._repos.activities.create(
                conn,
                activity_type=activity_type,
                summary=summary,
                company_id=company_id,
                contact_id=contact_id,
                metadata=metadata,
            )
            if actor is not None:
                audit_service.record_pipeline_activity(
                    conn,
                    actor_context=_actor_context(actor, correlation_id, company_id),
                    company_id=str(company_id),
                    summary_after={
                        "activity_type": activity_type,
                        "summary": summary,
                        "activity_id": str(activity["id"]),
                    },
                    repository=get_repositories().audit_events,
                )
        return activity

    def transition_company_stage(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        to_stage: str,
        actor: str,
        reason: str | None = None,
        confirm: bool = False,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            raise InvalidStageError(f"Company not found: {company_id}")

        from_stage = str(company.get("pipeline_stage") or "researching")
        validate_transition(from_stage, to_stage, confirm=confirm, reason=reason)

        with crm_transaction(conn):
            stage_reason = reason if to_stage in {"lost", "nurture"} else None
            clear_reason = to_stage not in {"lost", "nurture"}
            updated = self._repos.companies.update(
                conn,
                company_id,
                pipeline_stage=to_stage,
                stage_reason=stage_reason,
                clear_stage_reason=clear_reason and stage_reason is None,
            )
            if updated is None:
                raise InvalidStageError(f"Company not found: {company_id}")

            history = self._repos.stage_history.create(
                conn,
                company_id=company_id,
                from_stage=from_stage,
                to_stage=to_stage,
                changed_by=actor,
                reason=reason,
            )
            self._repos.activities.create(
                conn,
                activity_type="status_change",
                summary=f"Stage changed from {from_stage} to {to_stage}",
                company_id=company_id,
                metadata={
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                    "reason": reason,
                },
            )
            audit_service.record_pipeline_stage_change(
                conn,
                actor_context=_actor_context(actor, correlation_id, company_id),
                company_id=str(company_id),
                summary_before={"pipeline_stage": from_stage},
                summary_after={"pipeline_stage": to_stage, "reason": reason},
                metadata={
                    "confirm": confirm,
                    "history_id": str(history["id"]),
                },
                repository=get_repositories().audit_events,
            )

        return {"company": updated, "history": history}

    def update_company_next_action(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        actor: str,
        next_action: str | None = None,
        due_at: datetime | None = None,
        owner: str | None = None,
        expected_value: float | None = None,
        clear_due_at: bool = False,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            raise InvalidStageError(f"Company not found: {company_id}")

        with crm_transaction(conn):
            updated = self._repos.companies.update(
                conn,
                company_id,
                next_action=next_action,
                next_action_due_at=due_at,
                clear_next_action_due_at=clear_due_at,
                owner=owner,
                expected_value=expected_value,
            )
            if updated is None:
                raise InvalidStageError(f"Company not found: {company_id}")

            audit_service.record_pipeline_next_action_update(
                conn,
                actor_context=_actor_context(actor, correlation_id, company_id),
                company_id=str(company_id),
                summary_before={
                    "next_action": company.get("next_action"),
                    "next_action_due_at": _serialize_dt(company.get("next_action_due_at")),
                    "owner": company.get("owner"),
                    "expected_value": _serialize_decimal(company.get("expected_value")),
                },
                summary_after={
                    "next_action": updated.get("next_action"),
                    "next_action_due_at": _serialize_dt(updated.get("next_action_due_at")),
                    "owner": updated.get("owner"),
                    "expected_value": _serialize_decimal(updated.get("expected_value")),
                },
                repository=get_repositories().audit_events,
            )
        return updated

    def get_company_pipeline_detail(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
    ) -> dict[str, Any] | None:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            return None
        return {
            "company": company,
            "stage_history": self._repos.stage_history.list_for_company(conn, company_id),
            "activities": self._repos.activities.list_for_company(conn, company_id),
            "audit_events": get_repositories().audit_events.list_for_entity(
                conn,
                entity_type="company",
                entity_id=str(company_id),
            ),
        }

    def list_companies_by_stage(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if pipeline_stage is not None:
            validate_stage(pipeline_stage)
        return self._repos.companies.list_by_pipeline_stage(
            conn,
            pipeline_stage=pipeline_stage,
            limit=limit,
        )

    def list_overdue_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc)
        return self._repos.companies.list_overdue_actions(conn, as_of=reference, limit=limit)

    def list_upcoming_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime | None = None,
        within_days: int = 7,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc)
        until = reference + timedelta(days=within_days)
        return self._repos.companies.list_upcoming_actions(
            conn,
            as_of=reference,
            until=until,
            limit=limit,
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


def _actor_context(
    actor: str,
    correlation_id: str | None,
    company_id: UUID,
) -> ActorContext:
    return ActorContext(
        actor=actor,
        correlation_id=correlation_id or f"crm-{company_id}",
    )


def _serialize_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize_decimal(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


__all__ = [
    "ConfirmRequiredError",
    "CrmRepositories",
    "CrmService",
    "InvalidStageError",
    "InvalidTransitionError",
    "PipelineError",
    "ReasonRequiredError",
    "default_crm_repositories",
]
