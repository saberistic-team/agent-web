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
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None: ...

    def update(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        name: str | None = None,
        website: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_all(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...


class ContactRepository(Protocol):
    def create(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        normalized_profile_url: str | None = None,
        email: str | None = None,
        normalized_email: str | None = None,
        email_permission: str | None = None,
        email_provenance: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]: ...

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None: ...

    def update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        name: str | None = None,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        normalized_profile_url: str | None = None,
        email: str | None = None,
        normalized_email: str | None = None,
        email_permission: str | None = None,
        email_provenance: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        is_archived: bool | None = None,
    ) -> dict[str, Any] | None: ...

    def find_duplicates(
        self,
        conn: psycopg.Connection,
        *,
        normalized_profile_url: str | None = None,
        normalized_email: str | None = None,
        normalized_name: str | None = None,
        company_id: UUID | None = None,
        exclude_contact_id: UUID | None = None,
    ) -> dict[str, list[dict[str, Any]]]: ...

    def search(
        self,
        conn: psycopg.Connection,
        *,
        query: str = "",
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]: ...

    def set_buying_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        roles: list[str],
    ) -> list[str]: ...

    def get_buying_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> list[str]: ...


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
