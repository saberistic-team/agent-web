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
    ProjectBriefRepository,
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

    def list_all(
        self,
        conn: psycopg.Connection,
        *,
        limit: int = 200,
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


class PostgresContactRepository:
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
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contacts (
                    name, full_name, company_id, title, profile_url,
                    normalized_profile_url, email, normalized_email,
                    email_permission, email_provenance, last_interaction_at,
                    relationship_strength, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    name,
                    name,
                    company_id,
                    title,
                    profile_url,
                    normalized_profile_url,
                    email,
                    normalized_email,
                    email_permission,
                    email_provenance,
                    last_interaction_at,
                    relationship_strength,
                    notes,
                ),
            )
            row = cur.fetchone()
        return dict(row)

    def get_by_id(self, conn: psycopg.Connection, contact_id: UUID) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
            row = cur.fetchone()
        return dict(row) if row else None

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
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.extend(["name = %s", "full_name = %s"])
            values.extend([name, name])
        if company_id is not None:
            fields.append("company_id = %s")
            values.append(company_id)
        if title is not None:
            fields.append("title = %s")
            values.append(title)
        if profile_url is not None:
            fields.append("profile_url = %s")
            values.append(profile_url)
        if normalized_profile_url is not None:
            fields.append("normalized_profile_url = %s")
            values.append(normalized_profile_url)
        if email is not None:
            fields.append("email = %s")
            values.append(email or None)
        if normalized_email is not None:
            fields.append("normalized_email = %s")
            values.append(normalized_email)
        if email_permission is not None:
            fields.append("email_permission = %s")
            values.append(email_permission or None)
        if email_provenance is not None:
            fields.append("email_provenance = %s")
            values.append(email_provenance or None)
        if last_interaction_at is not None:
            fields.append("last_interaction_at = %s")
            values.append(last_interaction_at)
        if relationship_strength is not None:
            fields.append("relationship_strength = %s")
            values.append(relationship_strength or None)
        if notes is not None:
            fields.append("notes = %s")
            values.append(notes)
        if is_archived is not None:
            fields.append("is_archived = %s")
            values.append(is_archived)
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

    def find_duplicates(
        self,
        conn: psycopg.Connection,
        *,
        normalized_profile_url: str | None = None,
        normalized_email: str | None = None,
        normalized_name: str | None = None,
        company_id: UUID | None = None,
        exclude_contact_id: UUID | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        matches: dict[str, list[dict[str, Any]]] = {
            "profile_url": [],
            "email": [],
            "name_company": [],
        }
        with conn.cursor() as cur:
            if normalized_profile_url:
                sql = """
                    SELECT * FROM contacts
                    WHERE normalized_profile_url = %s
                """
                params: list[Any] = [normalized_profile_url]
                if exclude_contact_id:
                    sql += " AND id != %s"
                    params.append(exclude_contact_id)
                cur.execute(sql, params)
                matches["profile_url"] = [dict(row) for row in cur.fetchall()]

            if normalized_email:
                sql = """
                    SELECT * FROM contacts
                    WHERE normalized_email = %s
                """
                params = [normalized_email]
                if exclude_contact_id:
                    sql += " AND id != %s"
                    params.append(exclude_contact_id)
                cur.execute(sql, params)
                matches["email"] = [dict(row) for row in cur.fetchall()]

            if normalized_name and company_id:
                sql = """
                    SELECT * FROM contacts
                    WHERE LOWER(name) = %s AND company_id = %s
                """
                params = [normalized_name, company_id]
                if exclude_contact_id:
                    sql += " AND id != %s"
                    params.append(exclude_contact_id)
                cur.execute(sql, params)
                matches["name_company"] = [dict(row) for row in cur.fetchall()]
        return matches

    def search(
        self,
        conn: psycopg.Connection,
        *,
        query: str = "",
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        trimmed = query.strip()
        with conn.cursor() as cur:
            if trimmed:
                pattern = f"%{trimmed}%"
                sql = """
                    SELECT c.*, co.name AS company_name
                    FROM contacts c
                    LEFT JOIN companies co ON co.id = c.company_id
                    WHERE (
                        c.name ILIKE %s
                        OR c.email ILIKE %s
                        OR c.title ILIKE %s
                        OR c.profile_url ILIKE %s
                        OR co.name ILIKE %s
                    )
                """
                params: list[Any] = [pattern, pattern, pattern, pattern, pattern]
                if not include_archived:
                    sql += " AND c.is_archived = FALSE"
                sql += " ORDER BY c.updated_at DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, params)
            else:
                sql = """
                    SELECT c.*, co.name AS company_name
                    FROM contacts c
                    LEFT JOIN companies co ON co.id = c.company_id
                """
                params: list[Any] = []
                if not include_archived:
                    sql += " WHERE c.is_archived = FALSE"
                sql += " ORDER BY c.updated_at DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_for_company(
        self,
        conn: psycopg.Connection,
        company_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            sql = """
                SELECT * FROM contacts
                WHERE company_id = %s
            """
            params: list[Any] = [company_id]
            if not include_archived:
                sql += " AND is_archived = FALSE"
            sql += " ORDER BY name ASC"
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

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
            cur.execute(
                """
                SELECT role FROM contact_buying_roles
                WHERE contact_id = %s
                ORDER BY role
                """,
                (contact_id,),
            )
            stored = [str(row["role"]) for row in cur.fetchall()]
        return stored

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
                ORDER BY role
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
        "admin_users": repos.admin_users,
        "audit_events": repos.audit_events,
        "project_briefs": repos.project_briefs,
    }


CompanyRepo = PostgresCompanyRepository
ContactRepo = PostgresContactRepository
SourceRecordRepo = PostgresSourceRecordRepository
ActivityRepo = PostgresActivityRepository
AdminUserRepo = PostgresAdminUserRepository
