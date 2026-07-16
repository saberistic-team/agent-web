"""Repository interfaces for CRM persistence boundaries."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg

from app.patch import UNSET, MaybeUnset


class CompanyRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        website: str | None = None,
        status: str = "prospect",
        domain: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        headcount_estimate: int | None = None,
        funding_summary: str | None = None,
        target_status: str | None = None,
        last_verified_at: date | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...

    def list_all(
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
    ) -> list[dict[str, Any]]: ...

    def find_by_domain(
        self, conn: psycopg.Connection, domain: str, *, exclude_company_id: UUID | None = None
    ) -> list[dict[str, Any]]: ...

    def find_by_exact_name(
        self,
        conn: psycopg.Connection,
        name: str,
        *,
        exclude_company_id: UUID | None = None,
    ) -> list[dict[str, Any]]: ...

    def update(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        name: MaybeUnset[str] = UNSET,
        website: MaybeUnset[str] = UNSET,
        status: MaybeUnset[str] = UNSET,
        domain: MaybeUnset[str] = UNSET,
        category: MaybeUnset[str] = UNSET,
        stage: MaybeUnset[str] = UNSET,
        headcount_estimate: MaybeUnset[int] = UNSET,
        funding_summary: MaybeUnset[str] = UNSET,
        target_status: MaybeUnset[str] = UNSET,
        last_verified_at: MaybeUnset[date] = UNSET,
        notes: MaybeUnset[str] = UNSET,
    ) -> dict[str, Any] | None: ...

    def archive(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...

    def restore(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...


class ContactRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        full_name: str,
        email: str | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        email_permission: str | None = None,
        company_id: UUID | None = None,
        last_interaction_at: date | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
        field_sources: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None: ...

    def get_active_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
        *,
        exclude_contact_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Active-contact identity lookup — excludes archived rows (#226)."""
        ...

    def get_archived_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
    ) -> dict[str, Any] | None:
        """Archived-contact lookup — separate op for restore/review only (#226)."""
        ...

    def find_by_profile_url(
        self,
        conn: psycopg.Connection,
        profile_url: str,
        *,
        exclude_contact_id: UUID | None = None,
    ) -> list[dict[str, Any]]: ...

    def find_by_name_company(
        self,
        conn: psycopg.Connection,
        *,
        full_name: str,
        company_id: UUID,
        exclude_contact_id: UUID | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_all(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
        query: str | None = None,
        company_id: UUID | None = None,
        buying_role: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]: ...

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]: ...

    def update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        full_name: MaybeUnset[str] = UNSET,
        email: MaybeUnset[str] = UNSET,
        title: MaybeUnset[str] = UNSET,
        profile_url: MaybeUnset[str] = UNSET,
        email_permission: MaybeUnset[str] = UNSET,
        company_id: MaybeUnset[UUID] = UNSET,
        last_interaction_at: MaybeUnset[date] = UNSET,
        relationship_strength: MaybeUnset[str] = UNSET,
        notes: MaybeUnset[str] = UNSET,
        buying_roles: MaybeUnset[list[str]] = UNSET,
        field_sources: MaybeUnset[dict[str, Any]] = UNSET,
    ) -> dict[str, Any] | None: ...

    def count_active(self, conn: psycopg.Connection) -> int: ...

    def archive(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None: ...

    def restore(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None: ...


class SourceRecordRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        source_type: str,
        external_id: str | None = None,
        company_id: UUID | None = None,
        contact_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_by_source(
        self,
        conn: psycopg.Connection,
        *,
        source_type: str,
        external_id: str,
    ) -> dict[str, Any] | None: ...


class ActivityRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        activity_type: str,
        summary: str,
        company_id: UUID | None = None,
        contact_id: UUID | None = None,
        source_record_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...


class AdminUserRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        display_name: str | None = None,
        role: str = "viewer",
        is_active: bool = True,
    ) -> dict[str, Any]: ...

    def get_by_email(self, conn: psycopg.Connection, email: str) -> dict[str, Any] | None: ...

    def get_by_id(self, conn: psycopg.Connection, user_id: UUID) -> dict[str, Any] | None: ...


class ResearchRecordRepository(Protocol):
    def create(
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
        observed_at: datetime | None = None,
        confidence: float | None = None,
        review_at: datetime | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_for_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


class PipelineRepository(Protocol):
    def list_companies(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_company_pipeline(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None: ...

    def update_pipeline_fields(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        pipeline_stage: MaybeUnset[str] = UNSET,
        next_action: MaybeUnset[str] = UNSET,
        next_action_due_at: MaybeUnset[datetime] = UNSET,
        pipeline_owner: MaybeUnset[str] = UNSET,
        expected_value_cents: MaybeUnset[int] = UNSET,
        pipeline_loss_reason: MaybeUnset[str] = UNSET,
        pipeline_nurture_reason: MaybeUnset[str] = UNSET,
        clear_loss_reason: bool = False,
        clear_nurture_reason: bool = False,
    ) -> dict[str, Any] | None: ...

    def record_stage_history(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        from_stage: str | None,
        to_stage: str,
        changed_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_stage_history(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def list_overdue_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_upcoming_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        window_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_companies_without_next_action(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def count_by_pipeline_stage(
        self, conn: psycopg.Connection
    ) -> list[tuple[str, int]]: ...


class AcquisitionDashboardRepository(Protocol):
    def count_companies_by_dimension(
        self,
        conn: psycopg.Connection,
        dimension: str,
    ) -> list[tuple[str, int]]: ...

    def count_contacts_by_company_dimension(
        self,
        conn: psycopg.Connection,
        dimension: str,
    ) -> list[tuple[str, int]]: ...

    def list_overdue_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_upcoming_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        window_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_recent_evidence(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_stale_evidence(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_companies_without_decision_maker(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def list_companies_without_next_action(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class ProjectBriefRepository(Protocol):
    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
        query: str | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...

    def get_by_id(
        self,
        conn: psycopg.Connection,
        brief_id: int,
    ) -> dict[str, Any] | None: ...


class AuditEventRepository(Protocol):
    def append(
        self,
        conn: psycopg.Connection,
        *,
        actor: str,
        action: str,
        correlation_id: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        summary_before: dict[str, Any] | None = None,
        summary_after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]: ...

class ImportBatchRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        source_type: str,
        schema_version: str,
        checksum: str,
        actor: str,
        status: str,
        correlation_id: str,
        export_date: date | None = None,
        summary_counts: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, batch_id: UUID) -> dict[str, Any] | None: ...

    def get_committed_by_checksum(
        self, conn: psycopg.Connection, checksum: str
    ) -> dict[str, Any] | None: ...

    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]: ...

    def update_status(
        self,
        conn: psycopg.Connection,
        batch_id: UUID,
        *,
        status: str,
        summary_counts: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None: ...

    def create_row(
        self,
        conn: psycopg.Connection,
        *,
        batch_id: UUID,
        row_index: int,
        source_kind: str,
        source_identity: dict[str, Any],
        outcome: str,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        prior_snapshot: dict[str, Any] | None = None,
        applied_snapshot: dict[str, Any] | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]: ...

    def list_rows_for_batch(
        self,
        conn: psycopg.Connection,
        batch_id: UUID,
        *,
        outcome: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...
