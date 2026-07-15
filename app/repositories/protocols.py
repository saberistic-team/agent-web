"""Repository interfaces for CRM persistence boundaries."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg


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
        pipeline_stage: str = "researching",
        owner: str | None = None,
        expected_value: float | None = None,
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

    def list_by_pipeline_stage(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_overdue_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_upcoming_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime,
        until: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def update(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        name: str | None = None,
        website: str | None = None,
        status: str | None = None,
        domain: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        headcount_estimate: int | None = None,
        funding_summary: str | None = None,
        target_status: str | None = None,
        last_verified_at: date | None = None,
        notes: str | None = None,
        pipeline_stage: str | None = None,
        next_action: str | None = None,
        next_action_due_at: datetime | None = None,
        clear_next_action_due_at: bool = False,
        owner: str | None = None,
        expected_value: float | None = None,
        stage_reason: str | None = None,
        clear_stage_reason: bool = False,
    ) -> dict[str, Any] | None: ...

    def archive(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...

    def restore(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...


class ContactRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        full_name: str | None = None,
        company_id: UUID | None = None,
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None: ...

    def get_by_email(self, conn: psycopg.Connection, email: str) -> dict[str, Any] | None: ...

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


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


class StageHistoryRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        from_stage: str,
        to_stage: str,
        changed_by: str,
        reason: str | None = None,
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

    def list_for_entity(
        self,
        conn: psycopg.Connection,
        *,
        entity_type: str,
        entity_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...
