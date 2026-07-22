"""Postgres-backed CRM repository implementations."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

from app.contacts import DECISION_MAKER_BUYING_ROLES
from app.discovery.repository import PostgresDiscoveryRunRepository
from app.patch import UNSET, MaybeUnset
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
        field_sources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (
                    name, website, status, domain, category, stage,
                    headcount_estimate, funding_summary, target_status,
                    last_verified_at, notes, field_sources
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    name, website, status, domain, category, stage,
                    headcount_estimate, funding_summary, target_status,
                    last_verified_at, notes,
                    json.dumps(field_sources or {}),
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

    def find_by_exact_name(
        self,
        conn: psycopg.Connection,
        name: str,
        *,
        exclude_company_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["LOWER(name) = LOWER(%s)", "archived_at IS NULL"]
        params: list[Any] = [name.strip()]
        if exclude_company_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_company_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM companies
                WHERE {' AND '.join(conditions)}
                ORDER BY name ASC
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

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
        field_sources: MaybeUnset[dict[str, Any]] = UNSET,
    ) -> dict[str, Any] | None:
        """Apply a partial patch.

        A parameter left at :data:`UNSET` is omitted from the ``UPDATE`` and keeps
        its stored value. An explicit ``None`` writes SQL ``NULL`` (clear); any
        other value replaces the column.
        """
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("name", name),
            ("website", website),
            ("status", status),
            ("domain", domain),
            ("category", category),
            ("stage", stage),
            ("headcount_estimate", headcount_estimate),
            ("funding_summary", funding_summary),
            ("target_status", target_status),
            ("last_verified_at", last_verified_at),
            ("notes", notes),
            ("field_sources", field_sources),
        ):
            if value is UNSET:
                continue
            fields.append(f"{column} = %s")
            if column == "field_sources":
                values.append(json.dumps(value or {}))
            else:
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
        field_sources: dict[str, Any] | None = None,
        relationship_metrics: dict[str, Any] | None = None,
        crm_context_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (
                    full_name, email, title, profile_url, email_permission,
                    company_id, last_interaction_at, relationship_strength,
                    notes, buying_roles, field_sources, relationship_metrics,
                    crm_context_tags
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
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
                    json.dumps(field_sources or {}),
                    json.dumps(relationship_metrics or {}),
                    crm_context_tags or [],
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_active_by_id_for_update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> dict[str, Any] | None:
        """Return one active contact row locked for update (brief conversion #274)."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, co.name AS company_name
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                WHERE c.id = %s AND c.archived_at IS NULL
                FOR UPDATE OF c
                """,
                (contact_id,),
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
        """Return the single active (non-archived) contact for an email, if any.

        Active identity lookup: excludes archived rows (``archived_at IS NULL``).
        The partial unique index ``idx_contacts_email_unique`` guarantees at most
        one active row per normalized email; ``ORDER BY id`` keeps the result
        deterministic even if that guarantee is ever weakened.
        """
        normalized = email.strip().lower()
        conditions = ["LOWER(c.email) = %s", "c.archived_at IS NULL"]
        params: list[Any] = [normalized]
        if exclude_contact_id is not None:
            conditions.append("c.id <> %s")
            params.append(exclude_contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.*, co.name AS company_name
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                WHERE {' AND '.join(conditions)}
                ORDER BY c.id ASC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_archived_by_email(
        self,
        conn: psycopg.Connection,
        email: str,
    ) -> dict[str, Any] | None:
        """Return the best archived contact match for an email, if any.

        Archived identity lookup: the deliberate counterpart to
        ``get_active_by_email`` (issue #226). Archived rows are never silently
        linked as an active CRM contact — callers surface this only as a
        restore/review option. The most recently archived row wins so operators
        review the freshest history first.
        """
        normalized = email.strip().lower()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, co.name AS company_name
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                WHERE LOWER(c.email) = %s AND c.archived_at IS NOT NULL
                ORDER BY c.archived_at DESC, c.id
                LIMIT 1
                """,
                (normalized,),
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
        relationship_metrics: MaybeUnset[dict[str, Any]] = UNSET,
        crm_context_tags: MaybeUnset[list[str]] = UNSET,
    ) -> dict[str, Any] | None:
        """Apply a partial patch.

        A parameter left at :data:`UNSET` is omitted and keeps its stored value.
        An explicit ``None`` writes SQL ``NULL`` (clear) — e.g. clearing an email
        or disassociating a company — and any other value replaces the column.
        """
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
            ("buying_roles", buying_roles),
        ):
            if value is UNSET:
                continue
            fields.append(f"{column} = %s")
            values.append(value)
        if field_sources is not UNSET:
            fields.append("field_sources = %s::jsonb")
            values.append(json.dumps(field_sources))
        if relationship_metrics is not UNSET:
            fields.append("relationship_metrics = %s::jsonb")
            values.append(json.dumps(relationship_metrics))
        if crm_context_tags is not UNSET:
            fields.append("crm_context_tags = %s")
            values.append(crm_context_tags)
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

    def count_active(self, conn: psycopg.Connection) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM contacts WHERE archived_at IS NULL")
            row = cur.fetchone()
        return int(row["total"]) if row else 0

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

    def update_payload(
        self,
        conn: psycopg.Connection,
        *,
        record_id: UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE source_records
                SET payload = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (json.dumps(payload), _now(), record_id),
            )
            row = cur.fetchone()
        return dict(row) if row else {}


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

    def update_freshness(
        self,
        conn: psycopg.Connection,
        *,
        record_id: UUID,
        observed_at: datetime | None,
        confidence: float | None,
        review_at: datetime | None,
        expires_at: datetime | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE research_records
                SET observed_at = %s,
                    confidence = %s,
                    review_at = %s,
                    expires_at = %s,
                    metadata = COALESCE(%s, metadata),
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    observed_at,
                    confidence,
                    review_at,
                    expires_at,
                    json.dumps(metadata) if metadata is not None else None,
                    _now(),
                    record_id,
                ),
            )
            row = cur.fetchone()
        return dict(row) if row else None


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
        pipeline_stage: MaybeUnset[str] = UNSET,
        next_action: MaybeUnset[str] = UNSET,
        next_action_due_at: MaybeUnset[datetime] = UNSET,
        pipeline_owner: MaybeUnset[str] = UNSET,
        expected_value_cents: MaybeUnset[int] = UNSET,
        pipeline_loss_reason: MaybeUnset[str] = UNSET,
        pipeline_nurture_reason: MaybeUnset[str] = UNSET,
        clear_loss_reason: bool = False,
        clear_nurture_reason: bool = False,
    ) -> dict[str, Any] | None:
        """Apply a partial pipeline patch.

        A parameter left at :data:`UNSET` is omitted and keeps its stored value;
        an explicit ``None`` writes SQL ``NULL`` (clear); any other value replaces
        the column. The ``clear_loss_reason``/``clear_nurture_reason`` flags force
        a ``NULL`` write for stage-driven resets and are mutually exclusive with
        supplying that same reason as a value.
        """
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
            if value is UNSET:
                continue
            fields.append(f"{column} = %s")
            values.append(value)
        if clear_loss_reason and pipeline_loss_reason is UNSET:
            fields.append("pipeline_loss_reason = NULL")
        if clear_nurture_reason and pipeline_nurture_reason is UNSET:
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

    def list_due_today_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        day_start: datetime,
        day_end: datetime,
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
                  AND BTRIM(next_action) <> ''
                  AND next_action_due_at IS NOT NULL
                  AND next_action_due_at >= %s
                  AND next_action_due_at < %s
                ORDER BY next_action_due_at ASC
                LIMIT %s
                """,
                (day_start, day_end, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresActionQueueRepository:
    """Acquisition action queue queries — composes pipeline repo where needed."""

    _PUBLIC_EVIDENCE_TYPES = ("verified_fact", "public_signal")
    _WARM_STRENGTHS = ("warm", "strong", "champion")
    _TIER_A_STAGES = ("qualified", "ready_for_outreach")
    _DECISION_MAKER_ROLES = ("founder", "technical_buyer", "executive_buyer")

    def __init__(
        self,
        pipeline_repo: PostgresPipelineRepository | None = None,
    ) -> None:
        self._pipeline = pipeline_repo or PostgresPipelineRepository()

    def list_overdue_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._pipeline.list_overdue_next_actions(
            conn, reference=reference, limit=limit
        )

    def list_due_today_next_actions(
        self,
        conn: psycopg.Connection,
        *,
        day_start: datetime,
        day_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self._pipeline.list_due_today_next_actions(
            conn, day_start=day_start, day_end=day_end, limit=limit
        )

    def list_recently_qualified_tier_a(
        self,
        conn: psycopg.Connection,
        *,
        since: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (c.id)
                       c.id, c.name, c.pipeline_stage, c.pipeline_owner,
                       c.expected_value_cents, h.changed_at AS qualified_at
                FROM companies c
                INNER JOIN pipeline_stage_history h ON h.company_id = c.id
                WHERE c.archived_at IS NULL
                  AND c.target_status = 'target'
                  AND c.pipeline_stage = ANY(%s)
                  AND h.to_stage = 'qualified'
                  AND h.changed_at >= %s
                ORDER BY c.id, h.changed_at DESC
                LIMIT %s
                """,
                (list(self._TIER_A_STAGES), since, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_warm_introduction_opportunities(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ct.id AS contact_id, ct.full_name AS contact_name,
                       ct.relationship_strength, c.id AS company_id, c.name AS company_name,
                       c.pipeline_stage, c.expected_value_cents
                FROM contacts ct
                INNER JOIN companies c ON c.id = ct.company_id
                WHERE c.archived_at IS NULL
                  AND ct.archived_at IS NULL
                  AND (
                      'introducer' = ANY(ct.buying_roles)
                      OR ct.relationship_strength = ANY(%s)
                  )
                  AND (
                      c.pipeline_stage IS NOT NULL
                      OR c.target_status IN ('target', 'watching')
                  )
                ORDER BY ct.relationship_strength DESC, c.name ASC, ct.full_name ASC
                LIMIT %s
                """,
                (list(self._WARM_STRENGTHS), limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_stale_high_value_evidence(
        self,
        conn: psycopg.Connection,
        *,
        reference: datetime,
        min_value_cents: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rr.*, c.name AS company_name, c.pipeline_stage,
                       c.expected_value_cents
                FROM research_records rr
                INNER JOIN companies c ON c.id = rr.company_id
                WHERE rr.record_type = ANY(%s)
                  AND rr.expires_at IS NOT NULL
                  AND rr.expires_at <= %s
                  AND c.archived_at IS NULL
                  AND (
                      c.expected_value_cents >= %s
                      OR c.target_status = 'target'
                  )
                ORDER BY rr.expires_at ASC, c.expected_value_cents DESC NULLS LAST
                LIMIT %s
                """,
                (
                    list(self._PUBLIC_EVIDENCE_TYPES),
                    reference,
                    min_value_cents,
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_export_candidates(
        self,
        conn: psycopg.Connection,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.name AS company_name, c.domain, c.pipeline_stage,
                       c.target_status, c.expected_value_cents, c.next_action,
                       c.next_action_due_at, c.category,
                       ct.full_name AS contact_name, ct.title AS contact_title,
                       ct.buying_roles, ct.relationship_strength,
                       ev.source_url AS evidence_source_url,
                       ev.confidence AS evidence_confidence,
                       ev.record_type AS evidence_type,
                       EXISTS (
                           SELECT 1 FROM contacts dm
                           WHERE dm.company_id = c.id
                             AND dm.archived_at IS NULL
                             AND dm.buying_roles && %s::text[]
                       ) AS has_decision_maker
                FROM companies c
                LEFT JOIN LATERAL (
                    SELECT ct2.*
                    FROM contacts ct2
                    WHERE ct2.company_id = c.id AND ct2.archived_at IS NULL
                    ORDER BY ct2.updated_at DESC NULLS LAST
                    LIMIT 1
                ) ct ON TRUE
                LEFT JOIN LATERAL (
                    SELECT rr.source_url, rr.confidence, rr.record_type
                    FROM research_records rr
                    WHERE rr.company_id = c.id
                      AND rr.record_type = ANY(%s)
                      AND rr.source_url IS NOT NULL
                    ORDER BY rr.created_at DESC
                    LIMIT 1
                ) ev ON TRUE
                WHERE c.archived_at IS NULL
                  AND c.pipeline_stage IS NOT NULL
                  AND c.pipeline_stage NOT IN ('lost', 'nurture')
                ORDER BY c.name ASC
                LIMIT %s
                """,
                (
                    list(self._DECISION_MAKER_ROLES),
                    list(self._PUBLIC_EVIDENCE_TYPES),
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


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


class PostgresAnalyticsDashboardRepository:
    _ATTRIBUTION_EVENT_NAMES = (
        "Landing Viewed",
        "Services Viewed",
        "Case Studies Viewed",
        "Case Study Viewed",
        "Insights Viewed",
        "Insight Viewed",
        "Brief Viewed",
        "Brief Form Started",
        "Lead Persisted",
        "Checkout Opened",
        "Payment Completed",
        "Contact Initiated",
    )

    def count_events_in_range(
        self,
        conn: psycopg.Connection,
        *,
        period_start: datetime,
        period_end: datetime,
        event_names: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_name, COUNT(*)::int AS total
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = ANY(%s)
                GROUP BY event_name
                ORDER BY total DESC, event_name ASC
                """,
                (period_start, period_end, list(event_names)),
            )
            rows = cur.fetchall()
        return [(str(row["event_name"]), int(row["total"])) for row in rows]

    def count_attribution_in_range(
        self,
        conn: psycopg.Connection,
        *,
        period_start: datetime,
        period_end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(attribution->>'utm_source'), ''), '(direct)') AS source,
                    COALESCE(NULLIF(TRIM(attribution->>'utm_medium'), ''), '(none)') AS medium,
                    COALESCE(NULLIF(TRIM(attribution->>'utm_campaign'), ''), '(none)') AS campaign,
                    COUNT(*)::int AS event_count
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = ANY(%s)
                GROUP BY 1, 2, 3
                ORDER BY event_count DESC, source ASC, medium ASC, campaign ASC
                LIMIT %s
                """,
                (
                    period_start,
                    period_end,
                    list(self._ATTRIBUTION_EVENT_NAMES),
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def count_content_engagement(
        self,
        conn: psycopg.Connection,
        *,
        period_start: datetime,
        period_end: datetime,
        event_name: str,
        slug_property: str,
        limit: int,
    ) -> list[tuple[str, int]]:
        if slug_property not in {"case_study_slug", "article_slug"}:
            raise ValueError(f"unsupported slug property: {slug_property}")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT properties->>%s AS slug, COUNT(*)::int AS views
                FROM analytics_events
                WHERE occurred_at >= %s
                  AND occurred_at < %s
                  AND event_name = %s
                  AND COALESCE(TRIM(properties->>%s), '') <> ''
                GROUP BY 1
                ORDER BY views DESC, slug ASC
                LIMIT %s
                """,
                (
                    slug_property,
                    period_start,
                    period_end,
                    event_name,
                    slug_property,
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [(str(row["slug"]), int(row["views"])) for row in rows]

    def count_leads_by_utm_source(
        self,
        conn: psycopg.Connection,
        *,
        period_start: datetime,
        period_end: datetime,
        limit: int,
    ) -> list[tuple[str, int]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(utm_source), ''), '(direct)') AS source,
                    COUNT(*)::int AS total
                FROM project_briefs
                WHERE created_at >= %s
                  AND created_at < %s
                GROUP BY 1
                ORDER BY total DESC, source ASC
                LIMIT %s
                """,
                (period_start, period_end, limit),
            )
            rows = cur.fetchall()
        return [(str(row["source"]), int(row["total"])) for row in rows]


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
        payment_amount_cents, payment_discount_cents, payment_currency,
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


class PostgresIcpScoringRepository:
    def get_active_version(self, conn: psycopg.Connection) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, version_number, label, is_active, created_at, created_by
                FROM icp_scoring_versions
                WHERE is_active = TRUE
                ORDER BY version_number DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_version_by_number(
        self, conn: psycopg.Connection, version_number: int
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, version_number, label, is_active, created_at, created_by
                FROM icp_scoring_versions
                WHERE version_number = %s
                """,
                (version_number,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_rules_for_version(
        self, conn: psycopg.Connection, version_id: UUID
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id, version_id, dimension, label, weight, threshold,
                    enabled, accept_hypothesis, sort_order
                FROM icp_scoring_rules
                WHERE version_id = %s
                ORDER BY sort_order ASC, id ASC
                """,
                (version_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def create_version(
        self,
        conn: psycopg.Connection,
        *,
        version_number: int,
        label: str,
        created_by: str,
        activate: bool,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO icp_scoring_versions (
                    version_number, label, is_active, created_by
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id, version_number, label, is_active, created_at, created_by
                """,
                (version_number, label, activate, created_by),
            )
            row = cur.fetchone()
        return dict(row)

    def deactivate_all_versions(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("UPDATE icp_scoring_versions SET is_active = FALSE WHERE is_active = TRUE")

    def insert_rule(
        self,
        conn: psycopg.Connection,
        *,
        version_id: UUID,
        rule_id: str,
        dimension: str,
        label: str,
        weight: float,
        threshold: dict[str, Any],
        enabled: bool,
        accept_hypothesis: bool,
        sort_order: int,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO icp_scoring_rules (
                    id, version_id, dimension, label, weight, threshold,
                    enabled, accept_hypothesis, sort_order
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING
                    id, version_id, dimension, label, weight, threshold,
                    enabled, accept_hypothesis, sort_order
                """,
                (
                    rule_id,
                    version_id,
                    dimension,
                    label,
                    weight,
                    json.dumps(threshold),
                    enabled,
                    accept_hypothesis,
                    sort_order,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def insert_snapshot(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        version_id: UUID,
        version_number: int,
        total_score: float,
        computed_score: float,
        breakdown: list[dict[str, Any]],
        missing_inputs: list[str],
        calculated_at: datetime,
        is_override: bool = False,
        override_reason: str | None = None,
        override_by: str | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO company_icp_score_snapshots (
                    company_id, version_id, version_number, total_score, computed_score,
                    breakdown, missing_inputs, calculated_at, is_override,
                    override_reason, override_by
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    company_id,
                    version_id,
                    version_number,
                    total_score,
                    computed_score,
                    json.dumps(breakdown),
                    json.dumps(missing_inputs),
                    calculated_at,
                    is_override,
                    override_reason,
                    override_by,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_latest_snapshot_for_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM company_icp_score_snapshots
                WHERE company_id = %s
                ORDER BY calculated_at DESC, created_at DESC
                LIMIT 1
                """,
                (company_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_latest_snapshots(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (s.company_id)
                    s.*,
                    c.name AS company_name
                FROM company_icp_score_snapshots AS s
                JOIN companies AS c ON c.id = s.company_id
                WHERE c.archived_at IS NULL
                ORDER BY s.company_id, s.calculated_at DESC, s.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


class PostgresQualificationRepository:
    def list_active_companies(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM companies
                WHERE archived_at IS NULL
                ORDER BY name ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_latest_tier_for_company(
        self, conn: psycopg.Connection, company_id: UUID
    ) -> str | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_tier
                FROM qualification_tier_history
                WHERE company_id = %s
                ORDER BY changed_at DESC, id DESC
                LIMIT 1
                """,
                (company_id,),
            )
            row = cur.fetchone()
        return str(row["to_tier"]) if row else None

    def record_tier_change(
        self,
        conn: psycopg.Connection,
        *,
        company_id: UUID,
        from_tier: str | None,
        to_tier: str,
        score: float,
        changed_by: str,
        snapshot_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO qualification_tier_history (
                    company_id, from_tier, to_tier, score, changed_by, snapshot_id, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    company_id,
                    from_tier,
                    to_tier,
                    score,
                    changed_by,
                    snapshot_id,
                    json.dumps(metadata or {}),
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_tier_history(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.*, c.name AS company_name
                FROM qualification_tier_history AS h
                JOIN companies AS c ON c.id = h.company_id
                WHERE h.company_id = %s
                ORDER BY h.changed_at DESC, h.id DESC
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def create_working_list(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        owner: str,
        company_ids: list[UUID],
        max_items: int,
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO qualification_working_lists (name, owner, max_items)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (name, owner, max_items),
            )
            list_row = cur.fetchone()
            list_id = list_row["id"]
            for position, company_id in enumerate(company_ids):
                cur.execute(
                    """
                    INSERT INTO qualification_working_list_items (
                        list_id, company_id, position
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (list_id, company_id, position),
                )
        return dict(list_row)

    def list_working_lists_for_owner(
        self,
        conn: psycopg.Connection,
        *,
        owner: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    wl.*,
                    COUNT(wli.company_id)::int AS item_count
                FROM qualification_working_lists AS wl
                LEFT JOIN qualification_working_list_items AS wli
                    ON wli.list_id = wl.id
                WHERE wl.owner = %s
                GROUP BY wl.id
                ORDER BY wl.updated_at DESC, wl.created_at DESC
                LIMIT %s
                """,
                (owner, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_working_list_items(
        self,
        conn: psycopg.Connection,
        list_id: UUID,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT wli.company_id, wli.position, wli.added_at, c.name AS company_name
                FROM qualification_working_list_items AS wli
                JOIN companies AS c ON c.id = wli.company_id
                WHERE wli.list_id = %s
                ORDER BY wli.position ASC, wli.added_at ASC
                """,
                (list_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


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


class PostgresImportBatchRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_batches (
                    source_type, export_date, schema_version, checksum, actor,
                    status, summary_counts, error_message, correlation_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    source_type,
                    export_date,
                    schema_version,
                    checksum,
                    actor,
                    status,
                    json.dumps(summary_counts or {}),
                    error_message,
                    correlation_id,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, batch_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM import_batches WHERE id = %s", (batch_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_committed_by_checksum(
        self, conn: psycopg.Connection, checksum: str
    ) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM import_batches
                WHERE checksum = %s AND status = 'committed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (checksum,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        safe_page = max(page, 1)
        safe_per_page = max(min(per_page, 100), 1)
        offset = (safe_page - 1) * safe_per_page
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM import_batches")
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cur.execute(
                """
                SELECT * FROM import_batches
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (safe_per_page, offset),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows], total

    def update_status(
        self,
        conn: psycopg.Connection,
        batch_id: UUID,
        *,
        status: str,
        summary_counts: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        fields = ["status = %s", "updated_at = %s"]
        values: list[Any] = [status, _now()]
        if summary_counts is not None:
            fields.append("summary_counts = %s::jsonb")
            values.append(json.dumps(summary_counts))
        if error_message is not None:
            fields.append("error_message = %s")
            values.append(error_message)
        values.append(batch_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE import_batches
                SET {", ".join(fields)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        return dict(row) if row else None

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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_batch_rows (
                    batch_id, row_index, source_kind, source_identity, outcome,
                    entity_type, entity_id, prior_snapshot, applied_snapshot, detail
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                RETURNING *
                """,
                (
                    batch_id,
                    row_index,
                    source_kind,
                    json.dumps(source_identity),
                    outcome,
                    entity_type,
                    entity_id,
                    json.dumps(prior_snapshot) if prior_snapshot is not None else None,
                    json.dumps(applied_snapshot) if applied_snapshot is not None else None,
                    detail,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def list_rows_for_batch(
        self,
        conn: psycopg.Connection,
        batch_id: UUID,
        *,
        outcome: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["batch_id = %s"]
        params: list[Any] = [batch_id]
        if outcome:
            conditions.append("outcome = %s")
            params.append(outcome)
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM import_batch_rows
                WHERE {' AND '.join(conditions)}
                ORDER BY row_index ASC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]


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
        self.analytics_dashboard = PostgresAnalyticsDashboardRepository()
        self.action_queue = PostgresActionQueueRepository()
        self.pipeline = PostgresPipelineRepository()
        self.import_batches = PostgresImportBatchRepository()
        self.icp_scoring = PostgresIcpScoringRepository()
        self.qualification = PostgresQualificationRepository()
        self.discovery_runs = PostgresDiscoveryRunRepository()


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
        "analytics_dashboard": repos.analytics_dashboard,
        "action_queue": repos.action_queue,
        "pipeline": repos.pipeline,
        "import_batches": repos.import_batches,
        "icp_scoring": repos.icp_scoring,
        "qualification": repos.qualification,
        "discovery_runs": repos.discovery_runs,
    }


# Type aliases for consumers that want concrete defaults.
CompanyRepo = PostgresCompanyRepository
ContactRepo = PostgresContactRepository
SourceRecordRepo = PostgresSourceRecordRepository
ActivityRepo = PostgresActivityRepository
ResearchRecordRepo = PostgresResearchRecordRepository
AdminUserRepo = PostgresAdminUserRepository
