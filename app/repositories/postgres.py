"""Postgres-backed CRM repository implementations."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg

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
        email: str | None = None,
        full_name: str | None = None,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        profile_url_normalized: str | None = None,
        email_permission: str | None = None,
        email_source: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        roles = buying_roles or []
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (
                    email, full_name, company_id, title, profile_url,
                    profile_url_normalized, email_permission, email_source,
                    last_interaction_at, relationship_strength, notes, buying_roles
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    email,
                    full_name,
                    company_id,
                    title,
                    profile_url,
                    profile_url_normalized,
                    email_permission,
                    email_source,
                    last_interaction_at,
                    relationship_strength,
                    notes,
                    json.dumps(roles),
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
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM contacts
                WHERE LOWER(TRIM(email)) = LOWER(TRIM(%s)) AND archived_at IS NULL
                """,
                (email,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

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
            conditions.append("archived_at IS NULL")
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                "(full_name ILIKE %s OR email ILIKE %s OR title ILIKE %s OR profile_url ILIKE %s)"
            )
            params.extend((pattern, pattern, pattern, pattern))
        if company_id is not None:
            conditions.append("company_id = %s")
            params.append(company_id)
        if buying_role:
            conditions.append("buying_roles @> %s::jsonb")
            params.append(json.dumps([buying_role]))
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT contacts.*, companies.name AS company_name
                FROM contacts
                LEFT JOIN companies ON companies.id = contacts.company_id
                {where_sql}
                ORDER BY contacts.full_name ASC NULLS LAST, contacts.email ASC NULLS LAST
                LIMIT %s
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def find_duplicates(
        self,
        conn: psycopg.Connection,
        *,
        profile_url_normalized: str | None = None,
        email: str | None = None,
        full_name: str | None = None,
        company_id: UUID | None = None,
        exclude_contact_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["archived_at IS NULL"]
        params: list[Any] = []
        match_parts: list[str] = []
        if profile_url_normalized:
            match_parts.append("profile_url_normalized = %s")
            params.append(profile_url_normalized)
        if email:
            match_parts.append("LOWER(TRIM(email)) = LOWER(TRIM(%s))")
            params.append(email)
        if full_name and company_id is not None:
            match_parts.append(
                "company_id = %s AND LOWER(TRIM(full_name)) = LOWER(TRIM(%s))"
            )
            params.extend((company_id, full_name))
        if not match_parts:
            return []
        conditions.append(f"({' OR '.join(match_parts)})")
        if exclude_contact_id is not None:
            conditions.append("id <> %s")
            params.append(exclude_contact_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM contacts
                WHERE {' AND '.join(conditions)}
                ORDER BY full_name ASC NULLS LAST
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        email: str | None = None,
        full_name: str | None = None,
        company_id: UUID | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        profile_url_normalized: str | None = None,
        email_permission: str | None = None,
        email_source: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: str | None = None,
        notes: str | None = None,
        buying_roles: list[str] | None = None,
        set_company: bool = False,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("email", email),
            ("full_name", full_name),
            ("title", title),
            ("profile_url", profile_url),
            ("profile_url_normalized", profile_url_normalized),
            ("email_permission", email_permission),
            ("email_source", email_source),
            ("last_interaction_at", last_interaction_at),
            ("relationship_strength", relationship_strength),
            ("notes", notes),
        ):
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(value)
        if set_company:
            fields.append("company_id = %s")
            values.append(company_id)
        if buying_roles is not None:
            fields.append("buying_roles = %s")
            values.append(json.dumps(buying_roles))
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
        utm_source, utm_campaign
    """
    _DETAIL_COLUMNS = """
        id, created_at, website, contact_method, contact_value, brief, status,
        stripe_session_id, stripe_payment_intent_id, paid_at,
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
    }


# Type aliases for consumers that want concrete defaults.
CompanyRepo = PostgresCompanyRepository
ContactRepo = PostgresContactRepository
SourceRecordRepo = PostgresSourceRecordRepository
ActivityRepo = PostgresActivityRepository
ResearchRecordRepo = PostgresResearchRecordRepository
AdminUserRepo = PostgresAdminUserRepository
