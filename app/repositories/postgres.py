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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies (name, website, status)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (name, website, status),
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
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM companies
                ORDER BY name ASC
                LIMIT %s
                """,
                (limit,),
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


class PostgresContactRepository:
    _LIST_COLUMNS = """
        c.id, c.created_at, c.updated_at, c.company_id, c.email, c.full_name,
        c.title, c.profile_url, c.email_provenance, c.email_permission,
        c.last_interaction_at, c.relationship_strength, c.notes, c.status,
        co.name AS company_name
    """

    def create(
        self,
        conn: psycopg.Connection,
        *,
        full_name: str,
        company_id: UUID | None = None,
        email: str | None = None,
        title: str | None = None,
        profile_url: str | None = None,
        email_provenance: str | None = None,
        email_permission: str | None = None,
        last_interaction_at: datetime | None = None,
        relationship_strength: int | None = None,
        notes: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (
                    full_name, company_id, email, title, profile_url,
                    email_provenance, email_permission, last_interaction_at,
                    relationship_strength, notes, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    full_name,
                    company_id,
                    email,
                    title,
                    profile_url,
                    email_provenance,
                    email_permission,
                    last_interaction_at,
                    relationship_strength,
                    notes,
                    status,
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
            cur.execute("SELECT * FROM contacts WHERE email = %s", (email,))
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
        status_clause = "" if include_archived else " AND status = 'active'"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM contacts
                WHERE company_id = %s{status_clause}
                ORDER BY full_name ASC NULLS LAST, email ASC NULLS LAST
                LIMIT %s
                """,
                (company_id, limit),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def _build_list_filters(
        self,
        *,
        query: str | None,
        include_archived: bool,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if not include_archived:
            conditions.append("c.status = 'active'")
        if query:
            pattern = f"%{query}%"
            conditions.append(
                """(
                    c.full_name ILIKE %s
                    OR c.email ILIKE %s
                    OR c.title ILIKE %s
                    OR c.profile_url ILIKE %s
                    OR co.name ILIKE %s
                )"""
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        if not conditions:
            return "", params
        return " WHERE " + " AND ".join(conditions), params

    def list_page(
        self,
        conn: psycopg.Connection,
        *,
        query: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where_sql, filter_params = self._build_list_filters(
            query=query,
            include_archived=include_archived,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                {where_sql}
                """,
                filter_params,
            )
            total_row = cur.fetchone()
            total = int(total_row["total"]) if total_row else 0
            list_params = [*filter_params, limit, offset]
            cur.execute(
                f"""
                SELECT {self._LIST_COLUMNS}
                FROM contacts c
                LEFT JOIN companies co ON co.id = c.company_id
                {where_sql}
                ORDER BY c.full_name ASC NULLS LAST, c.email ASC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                list_params,
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows, total

    def list_all_active(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM contacts
                WHERE status = 'active'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def update(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        *,
        full_name: str | None = None,
        company_id: UUID | None = None,
        clear_company: bool = False,
        email: str | None = None,
        clear_email: bool = False,
        title: str | None = None,
        clear_title: bool = False,
        profile_url: str | None = None,
        clear_profile_url: bool = False,
        email_provenance: str | None = None,
        clear_email_provenance: bool = False,
        email_permission: str | None = None,
        clear_email_permission: bool = False,
        last_interaction_at: datetime | None = None,
        clear_last_interaction: bool = False,
        relationship_strength: int | None = None,
        clear_relationship_strength: bool = False,
        notes: str | None = None,
        clear_notes: bool = False,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if full_name is not None:
            fields.append("full_name = %s")
            values.append(full_name)
        if clear_company:
            fields.append("company_id = NULL")
        elif company_id is not None:
            fields.append("company_id = %s")
            values.append(company_id)
        if clear_email:
            fields.append("email = NULL")
        elif email is not None:
            fields.append("email = %s")
            values.append(email)
        if clear_title:
            fields.append("title = NULL")
        elif title is not None:
            fields.append("title = %s")
            values.append(title)
        if clear_profile_url:
            fields.append("profile_url = NULL")
        elif profile_url is not None:
            fields.append("profile_url = %s")
            values.append(profile_url)
        if clear_email_provenance:
            fields.append("email_provenance = NULL")
        elif email_provenance is not None:
            fields.append("email_provenance = %s")
            values.append(email_provenance)
        if clear_email_permission:
            fields.append("email_permission = NULL")
        elif email_permission is not None:
            fields.append("email_permission = %s")
            values.append(email_permission)
        if clear_last_interaction:
            fields.append("last_interaction_at = NULL")
        elif last_interaction_at is not None:
            fields.append("last_interaction_at = %s")
            values.append(last_interaction_at)
        if clear_relationship_strength:
            fields.append("relationship_strength = NULL")
        elif relationship_strength is not None:
            fields.append("relationship_strength = %s")
            values.append(relationship_strength)
        if clear_notes:
            fields.append("notes = NULL")
        elif notes is not None:
            fields.append("notes = %s")
            values.append(notes)
        if status is not None:
            fields.append("status = %s")
            values.append(status)
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

    def set_buying_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
        roles: list[str],
    ) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM contact_buying_roles WHERE contact_id = %s",
                (contact_id,),
            )
            for role in roles:
                cur.execute(
                    """
                    INSERT INTO contact_buying_roles (contact_id, role)
                    VALUES (%s, %s)
                    ON CONFLICT (contact_id, role) DO NOTHING
                    """,
                    (contact_id, role),
                )
        return list(roles)

    def get_buying_roles(
        self,
        conn: psycopg.Connection,
        contact_id: UUID,
    ) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role FROM contact_buying_roles
                WHERE contact_id = %s
                ORDER BY role ASC
                """,
                (contact_id,),
            )
            rows = cur.fetchall()
        return [str(row["role"]) for row in rows]


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
            conn.commit()
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
