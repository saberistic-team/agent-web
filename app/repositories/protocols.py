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
