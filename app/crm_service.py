"""CRM service boundary — orchestrates repositories without embedding SQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

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
    AuditEventRepository,
    CompanyRepository,
    ContactRepository,
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
    PostgresStageHistoryRepository,
    SourceRecordRepository,
    StageHistoryRepository,
)


@dataclass(frozen=True)
class CrmRepositories:
    companies: CompanyRepository
    contacts: ContactRepository
    source_records: SourceRecordRepository
    activities: ActivityRepository
    stage_history: StageHistoryRepository
    audit_events: AuditEventRepository
    admin_users: AdminUserRepository


def default_crm_repositories() -> CrmRepositories:
    return CrmRepositories(
        companies=PostgresCompanyRepository(),
        contacts=PostgresContactRepository(),
        source_records=PostgresSourceRecordRepository(),
        activities=PostgresActivityRepository(),
        stage_history=PostgresStageHistoryRepository(),
        audit_events=PostgresAuditEventRepository(),
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
                self._repos.audit_events.create(
                    conn,
                    entity_type="company",
                    entity_id=company_id,
                    action="activity_recorded",
                    actor=actor,
                    metadata={
                        "activity_type": activity_type,
                        "summary": summary,
                        "activity_id": str(activity["id"]),
                    },
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
    ) -> dict[str, Any]:
        company = self._repos.companies.get_by_id(conn, company_id)
        if company is None:
            raise InvalidStageError(f"Company not found: {company_id}")

        from_stage = str(company["pipeline_stage"])
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
            self._repos.audit_events.create(
                conn,
                entity_type="company",
                entity_id=company_id,
                action="stage_change",
                actor=actor,
                metadata={
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                    "reason": reason,
                    "confirm": confirm,
                    "history_id": str(history["id"]),
                },
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

            self._repos.audit_events.create(
                conn,
                entity_type="company",
                entity_id=company_id,
                action="next_action_updated",
                actor=actor,
                metadata={
                    "before": {
                        "next_action": company.get("next_action"),
                        "next_action_due_at": _serialize_dt(company.get("next_action_due_at")),
                        "owner": company.get("owner"),
                        "expected_value": _serialize_decimal(company.get("expected_value")),
                    },
                    "after": {
                        "next_action": updated.get("next_action"),
                        "next_action_due_at": _serialize_dt(updated.get("next_action_due_at")),
                        "owner": updated.get("owner"),
                        "expected_value": _serialize_decimal(updated.get("expected_value")),
                    },
                },
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
            "audit_events": self._repos.audit_events.list_for_entity(
                conn,
                entity_type="company",
                entity_id=company_id,
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
