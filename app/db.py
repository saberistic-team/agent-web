"""Render Postgres persistence for project briefs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Literal

import psycopg
from psycopg.rows import dict_row

BriefStatus = Literal["pending_payment", "paid", "abandoned"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project_briefs (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    website TEXT NOT NULL,
    contact_method TEXT NOT NULL DEFAULT 'email'
        CHECK (contact_method IN ('email')),
    contact_value TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment', 'paid', 'abandoned')),
    stripe_session_id TEXT,
    stripe_payment_intent_id TEXT,
    paid_at TIMESTAMPTZ,
    utm_source TEXT,
    utm_medium TEXT,
    utm_campaign TEXT,
    utm_content TEXT,
    utm_term TEXT
);
"""

MIGRATION_SQL = """
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_source TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_medium TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_campaign TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_content TEXT;
ALTER TABLE project_briefs ADD COLUMN IF NOT EXISTS utm_term TEXT;
"""

ADMIN_SESSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_sessions (
    id SERIAL PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    admin_username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS admin_sessions_token_hash_idx ON admin_sessions (token_hash);
"""


def init_db(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(MIGRATION_SQL)
            cur.execute(ADMIN_SESSIONS_SCHEMA_SQL)
        conn.commit()


@contextmanager
def db_connection(database_url: str) -> Generator[psycopg.Connection, None, None]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        yield conn


def create_brief(
    conn: psycopg.Connection,
    *,
    website: str,
    contact_method: str,
    contact_value: str,
    brief: str,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
    utm_content: str | None = None,
    utm_term: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_briefs (
                website, contact_method, contact_value, brief, status,
                utm_source, utm_medium, utm_campaign, utm_content, utm_term
            )
            VALUES (%s, %s, %s, %s, 'pending_payment', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                website,
                contact_method,
                contact_value,
                brief,
                utm_source,
                utm_medium,
                utm_campaign,
                utm_content,
                utm_term,
            ),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def update_brief_stripe_session(
    conn: psycopg.Connection,
    *,
    brief_id: int,
    stripe_session_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_briefs
            SET stripe_session_id = %s
            WHERE id = %s
            """,
            (stripe_session_id, brief_id),
        )
        conn.commit()


def get_brief_by_id(conn: psycopg.Connection, brief_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM project_briefs WHERE id = %s", (brief_id,))
        return cur.fetchone()


def mark_brief_paid(
    conn: psycopg.Connection,
    *,
    brief_id: int,
    stripe_session_id: str | None,
    stripe_payment_intent_id: str | None,
) -> dict[str, Any] | None:
    paid_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_briefs
            SET status = 'paid',
                stripe_session_id = COALESCE(%s, stripe_session_id),
                stripe_payment_intent_id = %s,
                paid_at = %s
            WHERE id = %s AND status != 'paid'
            RETURNING *
            """,
            (stripe_session_id, stripe_payment_intent_id, paid_at, brief_id),
        )
        row = cur.fetchone()
        conn.commit()
    return row


def create_admin_session(
    conn: psycopg.Connection,
    *,
    token_hash: str,
    admin_username: str,
    expires_at: datetime,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_sessions (token_hash, admin_username, expires_at)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (token_hash, admin_username, expires_at),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def get_admin_session_by_token_hash(
    conn: psycopg.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, token_hash, admin_username, created_at, expires_at, revoked_at
            FROM admin_sessions
            WHERE token_hash = %s
            """,
            (token_hash,),
        )
        return cur.fetchone()


def revoke_admin_session(conn: psycopg.Connection, *, token_hash: str) -> None:
    revoked_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_sessions
            SET revoked_at = %s
            WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (revoked_at, token_hash),
        )
        conn.commit()
