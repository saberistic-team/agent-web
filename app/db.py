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


def create_admin_session(
    conn: psycopg.Connection,
    *,
    token_hash: str,
    admin_username: str,
    expires_at: datetime,
    csrf_token_hash: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_sessions (token_hash, admin_username, expires_at, csrf_token_hash)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (token_hash, admin_username, expires_at, csrf_token_hash),
        )
        row = cur.fetchone()
    return int(row["id"])


def get_admin_session_by_token_hash(
    conn: psycopg.Connection,
    token_hash: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, token_hash, admin_username, created_at, expires_at, revoked_at,
                   csrf_token_hash
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


def update_admin_session_csrf(
    conn: psycopg.Connection,
    *,
    session_id: int,
    csrf_token_hash: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_sessions
            SET csrf_token_hash = %s
            WHERE id = %s AND revoked_at IS NULL
            """,
            (csrf_token_hash, session_id),
        )
        conn.commit()


def create_admin_login_flow(
    conn: psycopg.Connection,
    *,
    flow_token_hash: str,
    csrf_token_hash: str,
    expires_at: datetime,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_flows (flow_token_hash, csrf_token_hash, expires_at)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (flow_token_hash, csrf_token_hash, expires_at),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def get_admin_login_flow_by_token_hash(
    conn: psycopg.Connection,
    flow_token_hash: str,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, flow_token_hash, csrf_token_hash, created_at, expires_at, consumed_at
            FROM admin_login_flows
            WHERE flow_token_hash = %s
            """,
            (flow_token_hash,),
        )
        return cur.fetchone()


def claim_admin_login_flow(
    conn: psycopg.Connection,
    *,
    flow_token_hash: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Atomically mark one unconsumed, unexpired login flow as consumed.

    Returns the claimed row on success. A ``None`` result means the flow was
    missing, expired, already consumed, or lost a concurrent claim — callers
    must treat that as a failed security claim, never as success.
    """
    consumed_at = now
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_login_flows
            SET consumed_at = %s
            WHERE flow_token_hash = %s
              AND consumed_at IS NULL
              AND expires_at > %s
            RETURNING id, flow_token_hash, csrf_token_hash, created_at, expires_at, consumed_at
            """,
            (consumed_at, flow_token_hash, now),
        )
        row = cur.fetchone()
        conn.commit()
    return row


def cleanup_stale_admin_login_flows(
    conn: psycopg.Connection,
    *,
    now: datetime,
    expired_retention_seconds: int,
    consumed_retention_seconds: int,
    batch_size: int,
) -> int:
    """Delete expired and consumed login flows in a bounded batch.

    Active flows (unexpired and unconsumed) are never selected. Uses partial
    indexes on ``expires_at`` and ``consumed_at`` so normal login traffic does
    not scan the full table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM admin_login_flows
            WHERE id IN (
                SELECT id
                FROM admin_login_flows
                WHERE (
                    consumed_at IS NULL
                    AND expires_at < %s - make_interval(secs => %s)
                ) OR (
                    consumed_at IS NOT NULL
                    AND consumed_at < %s - make_interval(secs => %s)
                )
                ORDER BY id
                LIMIT %s
            )
            """,
            (
                now,
                expired_retention_seconds,
                now,
                consumed_retention_seconds,
                batch_size,
            ),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


def is_admin_login_throttled(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT locked_until
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            """,
            (limiter_key,),
        )
        row = cur.fetchone()
    if row is None:
        return False
    locked_until = row["locked_until"]
    if locked_until is None:
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > now


def record_admin_login_failure(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            ON CONFLICT (limiter_key) DO UPDATE SET
                failure_count = CASE
                    WHEN admin_login_rate_limits.window_started_at
                        < %s - make_interval(secs => %s)
                    THEN 1
                    ELSE admin_login_rate_limits.failure_count + 1
                END,
                window_started_at = CASE
                    WHEN admin_login_rate_limits.window_started_at
                        < %s - make_interval(secs => %s)
                    THEN %s
                    ELSE admin_login_rate_limits.window_started_at
                END,
                locked_until = CASE
                    WHEN (
                        CASE
                            WHEN admin_login_rate_limits.window_started_at
                                < %s - make_interval(secs => %s)
                            THEN 1
                            ELSE admin_login_rate_limits.failure_count + 1
                        END
                    ) >= %s
                    THEN %s + make_interval(secs => %s)
                    ELSE admin_login_rate_limits.locked_until
                END,
                updated_at = %s
            """,
            (
                limiter_key,
                now,
                now,
                now,
                window_seconds,
                now,
                window_seconds,
                now,
                now,
                window_seconds,
                rate_limit,
                now,
                lockout_seconds,
                now,
            ),
        )
        conn.commit()


def clear_admin_login_rate_limit(conn: psycopg.Connection, *, limiter_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM admin_login_rate_limits WHERE limiter_key = %s",
            (limiter_key,),
        )
        conn.commit()


def cleanup_expired_admin_login_rate_limits(
    conn: psycopg.Connection,
    *,
    now: datetime,
    window_seconds: int,
    lockout_seconds: int,
) -> int:
    retention_seconds = max(window_seconds, lockout_seconds) * 2
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM admin_login_rate_limits
            WHERE updated_at < %s - make_interval(secs => %s)
              AND (locked_until IS NULL OR locked_until < %s)
            """,
            (now, retention_seconds, now),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted
