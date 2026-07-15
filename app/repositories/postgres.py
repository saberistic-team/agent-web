"""Postgres-backed CRM repository implementations."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

from app.contacts import DECISION_MAKER_BUYING_ROLES
from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    AuditEventRepository,
    CompanyRepository,
    ContactRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
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
        domain: str | None = None,
        category: str | None = None,
        stage: str | None = None,
        headcount_estimate: int | None = None,
        funding_summary: str | None = None,
        target_status: str | None = None,
        last_verified_at: date | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (
                    name, website, status, domain, category, stage,
                    headcount_estimate, funding_summary, target_status,
                    last_verified_at, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    name, website, status, domain, category, stage,
                    headcount_estimate, funding_summary, target_status,
                    last_verified_at, notes,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            row = cur.fetchone()
        return dict(row) if row else None

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
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_archived:
            conditions.append("archived_at IS NULL")
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append("(name ILIKE %s OR domain ILIKE %s OR website ILIKE %s)")
            params.extend((pattern, pattern, pattern))
        for column, value in (
            ("category", category),
            ("stage", stage),
            ("target_status", target_status),
        ):
            if value:
                conditions.append(f"{column} = %s")
                params.append(value)
        if freshness == "fresh":
            conditions.append("last_verified_at >= CURRENT_DATE - INTERVAL '30 days'")
        elif freshness == "stale":
            conditions.append(
                "last_verified_at IS NOT NULL AND last_verified_at < CURRENT_DATE - INTERVAL '90 days'"
            )
        elif freshness == "unknown":
            conditions.append("last_verified_at IS NULL")
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM companies
                {where_sql}
                ORDER BY name ASC
                LIMIT %s
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def find_by_domain(
        self,
        conn: psycopg.Connection,
        domain: str,
        *,
        exclude_company_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["domain = %s", "archived_at IS NULL"]
        params: list[Any] = [domain]
        if exclude_company_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_company_id)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM companies WHERE {' AND '.join(conditions)} ORDER BY name ASC",
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

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
        for column, value in (
            ("domain", domain),
            ("category", category),
            ("stage", stage),
            ("headcount_estimate", headcount_estimate),
            ("funding_summary", funding_summary),
            ("target_status", target_status),
            ("last_verified_at", last_verified_at),
            ("notes", notes),
        ):
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(value)
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

    def archive(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE companies SET archived_at = %s, updated_at = %s
                WHERE id = %s AND archived_at IS NULL
                RETURNING *
                """,
                (_now(), _now(), company_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def restore(self, conn: psycopg.Connection, company_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE companies SET archived_at = NULL, updated_at = %s
                WHERE id = %s AND archived_at IS NOT NULL
                RETURNING *
                """,
                (_now(), company_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None


class PostgresContactRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (
                    full_name, email, title, profile_url, email_permission,
                    company_id, last_interaction_at, relationship_strength,
                    notes, buying_roles
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    full_name,
                    email,
                    title,
                    profile_url,
                    email_permission,
                    company_id,
                    last_interaction_at,
                    relationship_strength,
                    notes,
                    buying_roles or [],
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_by_email(self, conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
        normalized = email.strip().lower()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM contacts WHERE lower(email) = %s",
                (normalized,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_active_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
        *,
        exclude_contact_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        normalized = email.strip().lower()
        conditions = ["LOWER(email) = %s", "archived_at IS NULL"]
        params: list[Any] = [normalized]
        if exclude_contact_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.*, co.name AS company_name
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                WHERE {' AND '.join(conditions)}
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def find_by_profile_url(
        self,
        conn: psycopg.Connection,
        profile_url: str,
        *,
        exclude_contact_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["profile_url = %s", "archived_at IS NULL"]
        params: list[Any] = [profile_url]
        if exclude_contact_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM contacts
                WHERE {' AND '.join(conditions)}
                ORDER BY full_name ASC
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def find_by_name_company(
        self,
        conn: psycopg.Connection,
        *,
        full_name: str,
        company_id: UUID,
        exclude_contact_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [
            "company_id = %s",
            "LOWER(full_name) = LOWER(%s)",
            "archived_at IS NULL",
        ]
        params: list[Any] = [company_id, full_name]
        if exclude_contact_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM contacts
                WHERE {' AND '.join(conditions)}
                ORDER BY full_name ASC
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_all(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
        query: str | None = None,
        company_id: UUID | None = None,
        buying_role: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_archived:
            conditions.append("c.archived_at IS NULL")
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                "(c.full_name ILIKE %s OR c.email ILIKE %s OR c.title ILIKE %s OR c.profile_url ILIKE %s)"
            )
            params.extend((pattern, pattern, pattern, pattern))
        if company_id is not None:
            conditions.append("c.company_id = %s")
            params.append(company_id)
        if buying_role:
            conditions.append("%s = ANY(c.buying_roles)")
            params.append(buying_role)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.*, co.name AS company_name
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                {where_sql}
                ORDER BY c.full_name ASC NULLS LAST, c.email ASC NULLS LAST
                LIMIT %s
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = ["company_id = %s"]
        params: list[Any] = [company_id]
        if not include_archived:
            conditions.append("archived_at IS NULL")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM contacts
                WHERE {' AND '.join(conditions)}
                ORDER BY full_name ASC NULLS LAST, email ASC NULLS LAST
                LIMIT %s
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        full_name: str | None = None,
        email: str | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        email_permission: str | None = None,
        company_id: UUID | None = None,
        last_interaction_at: date | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("full_name", full_name),
            ("email", email),
            ("title", title),
            ("profile_url", profile_url),
            ("email_permission", email_permission),
            ("company_id", company_id),
            ("last_interaction_at", last_interaction_at),
            ("relationship_strength", relationship_strength),
            ("notes", notes),
        ):
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(value)
        if buying_roles is not None:
            fields.append("buying_roles = %s")
            values.append(buying_roles)
        if not fields:
            return self.get_by_id(conn, contact_id)

        fields.append("updated_at = %s")
        values.append(_now())
        values.append(contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE contacts
                SET {", ".join(fields)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def archive(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE contacts SET archived_at = %s, updated_at = %s
                WHERE id = %s AND archived_at IS NULL
                RETURNING *
                """,
                (_now(), _now(), contact_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def restore(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE contacts SET archived_at = NULL, updated_at = %s
                WHERE id = %s AND archived_at IS NOT NULL
                RETURNING *
                """,
                (_now(), contact_id),
            )
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


class PostgresResearchRecordRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_records (
                    record_type, company_id, contact_id, body,
                    source_name, source_url, observed_value, observed_at,
                    confidence, review_at, expires_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    record_type,
                    company_id,
                    contact_id,
                    body,
                    source_name,
                    source_url,
                    observed_value,
                    observed_at,
                    confidence,
                    review_at,
                    expires_at,
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
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM research_records
                WHERE company_id = %s
                ORDER BY observed_at DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_for_contact(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM research_records
                WHERE contact_id = %s
                ORDER BY observed_at DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (contact_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresPipelineRepository:
    def list_companies(
        self,
        conn: psycopg.Connection,
        *,
        pipeline_stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["archived_at IS NULL", "pipeline_stage IS NOT NULL"]
        params: list[Any] = []
        if pipeline_stage:
            conditions.append("pipeline_stage = %s")
            params.append(pipeline_stage)
        where_sql = " AND ".join(conditions)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM companies
                WHERE {where_sql}
                ORDER BY next_action_due_at ASC NULLS LAST, name ASC
                LIMIT %s
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_company_pipeline(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM companies WHERE id = %s AND archived_at IS NULL",
                (company_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def update_pipeline_fields(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        pipeline_stage: str | None = None,
        next_action: str | None = None,
        next_action_due_at: datetime | None = None,
        pipeline_owner: str | None = None,
        expected_value_cents: int | None = None,
        pipeline_loss_reason: str | None = None,
        pipeline_nurture_reason: str | None = None,
        clear_loss_reason: bool = False,
        clear_nurture_reason: bool = False,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("pipeline_stage", pipeline_stage),
            ("next_action", next_action),
            ("next_action_due_at", next_action_due_at),
            ("pipeline_owner", pipeline_owner),
            ("expected_value_cents", expected_value_cents),
            ("pipeline_loss_reason", pipeline_loss_reason),
            ("pipeline_nurture_reason", pipeline_nurture_reason),
        ):
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(value)
        if clear_loss_reason:
            fields.append("pipeline_loss_reason = NULL")
        if clear_nurture_reason:
            fields.append("pipeline_nurture_reason = NULL")
        if not fields:
            return self.get_company_pipeline(conn, company_id)

        fields.append("updated_at = %s")
        values.append(_now())
        values.append(company_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE companies
                SET {", ".join(fields)}
                WHERE id = %s AND archived_at IS NULL
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def record_stage_history(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        from_stage: str | None,
        to_stage: str,
        changed_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_stage_history (
                    company_id, from_stage, to_stage, changed_by, metadata
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    company_id,
                    from_stage,
                    to_stage,
                    changed_by,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_stage_history(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM pipeline_stage_history
                WHERE company_id = %s
                ORDER BY changed_at DESC
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_overdue_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, pipeline_stage, next_action, next_action_due_at,
                       pipeline_owner, expected_value_cents
                FROM companies
                WHERE archived_at IS NULL
                  AND pipeline_stage IS NOT NULL
                  AND next_action IS NOT NULL
                  AND next_action_due_at IS NOT NULL
                  AND next_action_due_at < %s
                ORDER BY next_action_due_at ASC
                LIMIT %s
                """,
                (reference, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_upcoming_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        window_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, pipeline_stage, next_action, next_action_due_at,
                       pipeline_owner, expected_value_cents
                FROM companies
                WHERE archived_at IS NULL
                  AND pipeline_stage IS NOT NULL
                  AND next_action IS NOT NULL
                  AND next_action_due_at IS NOT NULL
                  AND next_action_due_at >= %s
                  AND next_action_due_at <= %s
                ORDER BY next_action_due_at ASC
                LIMIT %s
                """,
                (reference, window_end, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_companies_without_next_action(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, pipeline_stage, target_status, category, stage
                FROM companies
                WHERE archived_at IS NULL
                  AND pipeline_stage IS NOT NULL
                  AND (
                      next_action IS NULL
                      OR BTRIM(next_action) = ''
                      OR next_action_due_at IS NULL
                  )
                ORDER BY name ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def count_by_pipeline_stage(
        self, conn: psycopg.Connection
    ) -> list[tuple[str, int]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pipeline_stage AS bucket, COUNT(*)::int AS total
                FROM companies
                WHERE archived_at IS NULL AND pipeline_stage IS NOT NULL
                GROUP BY pipeline_stage
                ORDER BY total DESC, bucket ASC
                """,
            )
            rows = cur.fetchall()
        return [(str(row["bucket"]), int(row["total"])) for row in rows]


class PostgresAcquisitionDashboardRepository:
    _COMPANY_DIMENSIONS = frozenset({"stage", "category"})
    _PUBLIC_EVIDENCE_TYPES = ("verified_fact", "public_signal")
    _TARGET_STATUSES = ("target", "watching")

    def __init__(
        self,
        pipeline_repo: PostgresPipelineRepository | None = None,
    ) -> None:
        self._pipeline = pipeline_repo or PostgresPipelineRepository()

    def count_companies_by_dimension(
        self,
        conn: psycopg.Connection,
        dimension: str,
    ) -> list[tuple[str, int]]:
        if dimension not in self._COMPANY_DIMENSIONS:
            raise ValueError(f"unsupported company dimension: {dimension}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE({dimension}, 'unspecified') AS bucket, COUNT(*)::int AS total
                FROM companies
                WHERE archived_at IS NULL
                GROUP BY bucket
                ORDER BY total DESC, bucket ASC
                """,
            )
            rows = cur.fetchall()
        return [(str(row["bucket"]), int(row["total"])) for row in rows]

    def count_contacts_by_company_dimension(
        self,
        conn: psycopg.Connection,
        dimension: str,
    ) -> list[tuple[str, int]]:
        if dimension not in self._COMPANY_DIMENSIONS:
            raise ValueError(f"unsupported contact dimension: {dimension}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(c.{dimension}, 'unspecified') AS bucket, COUNT(*)::int AS total
                FROM contacts ct
                INNER JOIN companies c ON c.id = ct.company_id
                WHERE c.archived_at IS NULL
                  AND ct.archived_at IS NULL
                GROUP BY bucket
                ORDER BY total DESC, bucket ASC
                """,
            )
            rows = cur.fetchall()
        return [(str(row["bucket"]), int(row["total"])) for row in rows]

    def list_overdue_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._pipeline.list_overdue_next_actions(
            conn,
            reference=reference,
            limit=limit,
        )

    def list_upcoming_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        window_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._pipeline.list_upcoming_next_actions(
            conn,
            reference=reference,
            window_end=window_end,
            limit=limit,
        )

    def list_recent_evidence(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rr.*, c.name AS company_name
                FROM research_records rr
                INNER JOIN companies c ON c.id = rr.company_id
                WHERE rr.record_type = ANY(%s)
                  AND c.archived_at IS NULL
                ORDER BY rr.created_at DESC
                LIMIT %s
                """,
                (list(self._PUBLIC_EVIDENCE_TYPES), limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_stale_evidence(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rr.*, c.name AS company_name
                FROM research_records rr
                INNER JOIN companies c ON c.id = rr.company_id
                WHERE rr.record_type = ANY(%s)
                  AND rr.expires_at IS NOT NULL
                  AND rr.expires_at <= %s
                  AND c.archived_at IS NULL
                ORDER BY rr.expires_at ASC
                LIMIT %s
                """,
                (list(self._PUBLIC_EVIDENCE_TYPES), reference, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_companies_without_decision_maker(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*
                FROM companies c
                WHERE c.archived_at IS NULL
                  AND c.target_status = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM contacts ct
                      WHERE ct.company_id = c.id
                        AND ct.archived_at IS NULL
                        AND ct.buying_roles && %s::text[]
                  )
                ORDER BY c.name ASC
                LIMIT %s
                """,
                (list(self._TARGET_STATUSES), list(DECISION_MAKER_BUYING_ROLES), limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_companies_without_next_action(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._pipeline.list_companies_without_next_action(conn, limit=limit)


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



class PostgresProjectBriefRepository:
    _LIST_COLUMNS = """
        id, created_at, website, contact_value, status, paid_at,
        payment_amount_cents, payment_discount_cents,
        utm_source, utm_campaign
    """
    _DETAIL_COLUMNS = """
        id, created_at, website, contact_method, contact_value, brief, status,
        stripe_session_id, stripe_payment_intent_id, paid_at,
        payment_subtotal_cents, payment_discount_cents, payment_amount_cents,
        payment_currency, stripe_promotion_code_id, stripe_coupon_id,
        utm_source, utm_medium, utm_campaign, utm_content, utm_term
    """

    def _build_filters(
        self,
        *,
        query: str | None,
        status: str | None,
        date_from: date | None,
        date_to: date | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if query:
            if query.isdigit():
                conditions.append("id = %s")
                params.append(int(query))
            else:
                pattern = f"%{query}%"
                conditions.append("(website ILIKE %s OR contact_value ILIKE %s)")
                params.extend([pattern, pattern])
        if status:
            conditions.append("status = %s")
            params.append(status)
        if date_from is not None:
            start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
            conditions.append("created_at >= %s")
            params.append(start)
        if date_to is not None:
            end = datetime.combine(
                date_to + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
            conditions.append("created_at < %s")
            params.append(end)
        if not conditions:
            return "", params
        return " WHERE " + " AND ".join(conditions), params

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
    ) -> tuple[list[dict[str, Any]], int]:
        offset = (page - 1) * per_page
        where_sql, filter_params = self._build_filters(
            query=query,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS total FROM project_briefs{where_sql}",
                filter_params,
            )
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            list_params = [*filter_params, per_page, offset]
            cur.execute(
                f"""
                SELECT {self._LIST_COLUMNS}
                FROM project_briefs
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                list_params,
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows, total

    def get_by_id(
        self,
        conn: psycopg.Connection,
        brief_id: int,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {self._DETAIL_COLUMNS}
                FROM project_briefs
                WHERE id = %s
                """,
                (brief_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None


class PostgresAuditEventRepository:
    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        offset = (page - 1) * per_page
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM audit_events")
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cur.execute(
                """
                SELECT
                    id, created_at, actor, action, entity_type, entity_id,
                    correlation_id, summary_before, summary_after, metadata
                FROM audit_events
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (per_page, offset),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows, total

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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events (
                    actor, action, entity_type, entity_id, correlation_id,
                    summary_before, summary_after, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                RETURNING *
                """,
                (
                    actor,
                    action,
                    entity_type,
                    entity_id,
                    correlation_id,
                    json.dumps(summary_before) if summary_before is not None else None,
                    json.dumps(summary_after) if summary_after is not None else None,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )
            row = cur.fetchone()
        return dict(row)


class PostgresRepositories:
    """Bundle of Postgres repository implementations including CRM + audit."""

    def __init__(self) -> None:
        self.companies = PostgresCompanyRepository()
        self.contacts = PostgresContactRepository()
        self.source_records = PostgresSourceRecordRepository()
        self.activities = PostgresActivityRepository()
        self.research_records = PostgresResearchRecordRepository()
        self.admin_users = PostgresAdminUserRepository()
        self.audit_events = PostgresAuditEventRepository()
        self.project_briefs = PostgresProjectBriefRepository()
        self.acquisition_dashboard = PostgresAcquisitionDashboardRepository()
        self.pipeline = PostgresPipelineRepository()


_default_repositories = PostgresRepositories()


def get_repositories() -> PostgresRepositories:
    return _default_repositories


def default_repositories() -> dict[str, Any]:
    repos = get_repositories()
    return {
        "companies": repos.companies,
        "contacts": repos.contacts,
        "source_records": repos.source_records,
        "activities": repos.activities,
        "research_records": repos.research_records,
        "admin_users": repos.admin_users,
        "audit_events": repos.audit_events,
        "project_briefs": repos.project_briefs,
        "acquisition_dashboard": repos.acquisition_dashboard,
        "pipeline": repos.pipeline,
    }


# Type aliases for consumers that want concrete defaults.
CompanyRepo = PostgresCompanyRepository
ContactRepo = PostgresContactRepository
SourceRecordRepo = PostgresSourceRecordRepository
ActivityRepo = PostgresActivityRepository
ResearchRecordRepo = PostgresResearchRecordRepository
AdminUserRepo = PostgresAdminUserRepository
