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
    contact_method TEXT NOT NULL CHECK (contact_method IN ('email', 'phone')),
    contact_value TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment'
        CHECK (status IN ('pending_payment', 'paid', 'abandoned')),
    stripe_session_id TEXT,
    stripe_payment_intent_id TEXT,
    paid_at TIMESTAMPTZ
);
"""


def init_db(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
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
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_briefs (website, contact_method, contact_value, brief, status)
            VALUES (%s, %s, %s, %s, 'pending_payment')
            RETURNING id
            """,
            (website, contact_method, contact_value, brief),
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
