"""Render Postgres persistence for project briefs and CRM foundation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Literal

import psycopg
from psycopg.rows import dict_row

from app.migrations.runner import apply_migrations

BriefStatus = Literal["pending_payment", "paid", "abandoned"]


def init_db(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        apply_migrations(conn)


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
