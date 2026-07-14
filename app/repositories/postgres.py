"""Postgres-backed CRM repository implementations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg

from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    AuditEventRepository,
    CompanyRepository,
    ContactRepository,
    SourceRecordRepository,
    StageHistoryRepository,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PostgresCompanyRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        website: str | None = None,
        status: str = "prospect",
        pipeline_stage: str = "researching",
        owner: str | None = None,
        expected_value: float | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (
                    name, website, status, pipeline_stage, owner, expected_value
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (name, website, status, pipeline_stage, owner, expected_value),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def update(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        name: str | None = None,
        website: str | None = None,
        status: str | None = None,
        pipeline_stage: str | None = None,
        next_action: str | None = None,
        next_action_due_at: datetime | None = None,
        clear_next_action_due_at: bool = False,
        owner: str | None = None,
        expected_value: float | None = None,
        stage_reason: str | None = None,
        clear_stage_reason: bool = False,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.append("name = %s")
            values.append(name)
        if website is not None:
            fields.append("website = %s")
            values.append(website)
        if status is not None:
            fields.append("status = %s")
            values.append(status)
        if pipeline_stage is not None:
            fields.append("pipeline_stage = %s")
            values.append(pipeline_stage)
        if next_action is not None:
            fields.append("next_action = %s")
            values.append(next_action)
        if next_action_due_at is not None:
            fields.append("next_action_due_at = %s")
            values.append(next_action_due_at)
        elif clear_next_action_due_at:
            fields.append("next_action_due_at = NULL")
        if owner is not None:
            fields.append("owner = %s")
            values.append(owner)
        if expected_value is not None:
            fields.append("expected_value = %s")
            values.append(expected_value)
        if stage_reason is not None:
            fields.append("stage_reason = %s")
            values.append(stage_reason)
        elif clear_stage_reason:
            fields.append("stage_reason = NULL")
        if not fields:
            return self.get_by_id(conn, company_id)

        fields.append("updated_at = %s")
        values.append(_now())
        values.append(company_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE companies
                SET {", ".join(fields)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_by_pipeline_stage(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            if pipeline_stage is None:
                cur.execute(
                    """
                    SELECT * FROM companies
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM companies
                    WHERE pipeline_stage = %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (pipeline_stage, limit),
                )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_overdue_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM companies
                WHERE next_action IS NOT NULL
                  AND next_action_due_at IS NOT NULL
                  AND next_action_due_at < %s
                  AND pipeline_stage NOT IN ('won', 'lost')
                ORDER BY next_action_due_at ASC
                LIMIT %s
                """,
                (as_of, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_upcoming_actions(
        self,
        conn: psycopg.Connection,
        *,
        as_of: datetime,
        until: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM companies
                WHERE next_action IS NOT NULL
                  AND next_action_due_at IS NOT NULL
                  AND next_action_due_at >= %s
                  AND next_action_due_at <= %s
                  AND pipeline_stage NOT IN ('won', 'lost')
                ORDER BY next_action_due_at ASC
                LIMIT %s
                """,
                (as_of, until, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresContactRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        full_name: str | None = None,
        company_id: UUID | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (email, full_name, company_id)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (email, full_name, company_id),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_by_email(self, conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE email = %s", (email,))
            row = cur.fetchone()
        return dict(row) if row else None


class PostgresSourceRecordRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        source_type: str,
        external_id: str | None = None,
        company_id: UUID | None = None,
        contact_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_records (
                    source_type, external_id, company_id, contact_id, payload
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    source_type,
                    external_id,
                    company_id,
                    contact_id,
                    json.dumps(payload) if payload is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_source(
        self,
        conn: psycopg.Connection,
        *,
        source_type: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM source_records
                WHERE source_type = %s AND external_id = %s
                """,
                (source_type, external_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None


class PostgresActivityRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO activities (
                    activity_type, summary, company_id, contact_id,
                    source_record_id, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    activity_type,
                    summary,
                    company_id,
                    contact_id,
                    source_record_id,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM activities
                WHERE company_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresStageHistoryRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO company_stage_history (
                    company_id, from_stage, to_stage, changed_by, reason, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    company_id,
                    from_stage,
                    to_stage,
                    changed_by,
                    reason,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM company_stage_history
                WHERE company_id = %s
                ORDER BY changed_at DESC
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresAuditEventRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        entity_type: str,
        entity_id: UUID,
        action: str,
        actor: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO crm_audit_events (
                    entity_type, entity_id, action, actor, metadata
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    entity_type,
                    entity_id,
                    action,
                    actor,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_for_entity(
        self,
        conn: psycopg.Connection,
        *,
        entity_type: str,
        entity_id: UUID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM crm_audit_events
                WHERE entity_type = %s AND entity_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (entity_type, entity_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresAdminUserRepository:
    def create(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        display_name: str | None = None,
        role: str = "viewer",
        is_active: bool = True,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_users (email, display_name, role, is_active)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (email, display_name, role, is_active),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_email(self, conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM admin_users WHERE email = %s", (email,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_by_id(self, conn: psycopg.Connection, user_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM admin_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return dict(row) if row else None


def default_repositories() -> dict[str, Any]:
    return {
        "companies": PostgresCompanyRepository(),
        "contacts": PostgresContactRepository(),
        "source_records": PostgresSourceRecordRepository(),
        "activities": PostgresActivityRepository(),
        "stage_history": PostgresStageHistoryRepository(),
        "audit_events": PostgresAuditEventRepository(),
        "admin_users": PostgresAdminUserRepository(),
    }


# Type aliases for consumers that want concrete defaults.
CompanyRepo = PostgresCompanyRepository
ContactRepo = PostgresContactRepository
SourceRecordRepo = PostgresSourceRecordRepository
ActivityRepo = PostgresActivityRepository
StageHistoryRepo = PostgresStageHistoryRepository
AuditEventRepo = PostgresAuditEventRepository
AdminUserRepo = PostgresAdminUserRepository
