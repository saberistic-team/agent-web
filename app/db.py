"""Render Postgres persistence for project briefs and CRM foundation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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


def consume_admin_login_flow(conn: psycopg.Connection, *, flow_token_hash: str) -> None:
    consumed_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_login_flows
            SET consumed_at = %s
            WHERE flow_token_hash = %s AND consumed_at IS NULL
            """,
            (consumed_at, flow_token_hash),
        )
        conn.commit()


def update_admin_login_flow_csrf(
    conn: psycopg.Connection,
    *,
    flow_token_hash: str,
    csrf_token_hash: str,
) -> bool:
    """Rotate the CSRF hash for an active, unconsumed login flow."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_login_flows
            SET csrf_token_hash = %s
            WHERE flow_token_hash = %s
              AND consumed_at IS NULL
              AND expires_at > NOW()
            """,
            (csrf_token_hash, flow_token_hash),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


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
    """Return True when a single limiter bucket is in an active lockout window."""
    return is_admin_login_locked(conn, limiter_keys=[limiter_key], now=now)


def is_admin_login_locked(
    conn: psycopg.Connection,
    *,
    limiter_keys: list[str],
    now: datetime,
) -> bool:
    """Read-only check: True when any bucket is currently locked."""
    if not limiter_keys:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT locked_until
            FROM admin_login_rate_limits
            WHERE limiter_key = ANY(%s)
            """,
            (limiter_keys,),
        )
        rows = cur.fetchall()
    for row in rows:
        locked_until = row["locked_until"]
        if locked_until is None:
            continue
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            return True
    return False


def _upsert_admin_login_limiter_row(
    cur: psycopg.Cursor,
    *,
    limiter_key: str,
    failure_count: int,
    window_started_at: datetime,
    locked_until: datetime | None,
    updated_at: datetime,
) -> None:
    cur.execute(
        """
        INSERT INTO admin_login_rate_limits (
            limiter_key, failure_count, window_started_at, locked_until, updated_at
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (limiter_key) DO UPDATE SET
            failure_count = EXCLUDED.failure_count,
            window_started_at = EXCLUDED.window_started_at,
            locked_until = EXCLUDED.locked_until,
            updated_at = EXCLUDED.updated_at
        """,
        (
            limiter_key,
            failure_count,
            window_started_at,
            locked_until,
            updated_at,
        ),
    )


def _next_limiter_failure_state(
    row: dict[str, Any] | None,
    *,
    now: datetime,
    window_seconds: int,
    rate_limit: int,
    lockout_seconds: int,
) -> tuple[int, datetime, datetime | None, bool, bool]:
    """Compute the next bucket state for one failed or admitted attempt.

    Returns ``(failure_count, window_started_at, locked_until, admitted,
    lockout_transition)``. ``lockout_transition`` is True when this mutation
    newly enters an active lockout window.
    """
    window_cutoff = now.timestamp() - window_seconds
    prior_locked_until = None
    if row is not None:
        prior_locked_until = row["locked_until"]
        if prior_locked_until is not None and prior_locked_until.tzinfo is None:
            prior_locked_until = prior_locked_until.replace(tzinfo=timezone.utc)
        if prior_locked_until is not None and prior_locked_until > now:
            return (
                int(row["failure_count"]),
                row["window_started_at"],
                prior_locked_until,
                False,
                False,
            )
        window_started_at = row["window_started_at"]
        if window_started_at.tzinfo is None:
            window_started_at = window_started_at.replace(tzinfo=timezone.utc)
        if prior_locked_until is not None and prior_locked_until <= now:
            failure_count = 1
            window_started_at = now
        elif window_started_at.timestamp() < window_cutoff:
            failure_count = 1
            window_started_at = now
        else:
            failure_count = int(row["failure_count"]) + 1
    else:
        failure_count = 1
        window_started_at = now

    if failure_count > rate_limit:
        locked_until = now + timedelta(seconds=lockout_seconds)
        lockout_transition = prior_locked_until is None or prior_locked_until <= now
        return failure_count, window_started_at, locked_until, False, lockout_transition

    locked_until = None
    lockout_transition = False
    if failure_count >= rate_limit:
        locked_until = now + timedelta(seconds=lockout_seconds)
        lockout_transition = prior_locked_until is None or prior_locked_until <= now
    return failure_count, window_started_at, locked_until, True, lockout_transition


def admit_admin_login_attempt(
    conn: psycopg.Connection,
    *,
    limiter_keys: list[str],
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> tuple[bool, bool]:
    """Atomically reserve one password-verification attempt across buckets.

    Returns ``(admitted, lockout_transition)``. Concurrent callers cannot all pass
    based on the same pre-increment state because each bucket row is locked with
    ``SELECT … FOR UPDATE`` inside one transaction.
    """
    unique_keys = sorted(set(limiter_keys))
    if not unique_keys:
        return True, False

    lockout_transition = False
    with conn.transaction():
        rows_by_key: dict[str, dict[str, Any] | None] = {}
        computed: list[
            tuple[str, int, datetime, datetime | None, bool, bool]
        ] = []
        with conn.cursor() as cur:
            for limiter_key in unique_keys:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (limiter_key,))
                cur.execute(
                    """
                    SELECT failure_count, window_started_at, locked_until
                    FROM admin_login_rate_limits
                    WHERE limiter_key = %s
                    FOR UPDATE
                    """,
                    (limiter_key,),
                )
                row = cur.fetchone()
                rows_by_key[limiter_key] = row
                computed.append(
                    (limiter_key,)
                    + _next_limiter_failure_state(
                        row,
                        now=now,
                        window_seconds=window_seconds,
                        rate_limit=rate_limit,
                        lockout_seconds=lockout_seconds,
                    )
                )

        already_locked = any(
            row is not None
            and row.get("locked_until") is not None
            and (
                row["locked_until"].replace(tzinfo=timezone.utc)
                if row["locked_until"].tzinfo is None
                else row["locked_until"]
            )
            > now
            for row in rows_by_key.values()
        )
        if already_locked:
            return False, False

        denied = [item for item in computed if not item[4]]
        if denied:
            lockout_transition = any(item[5] for item in denied)
            with conn.cursor() as cur:
                for (
                    limiter_key,
                    failure_count,
                    window_started_at,
                    locked_until,
                    _admitted,
                    transitioned,
                ) in denied:
                    if not transitioned and failure_count <= rate_limit:
                        continue
                    _upsert_admin_login_limiter_row(
                        cur,
                        limiter_key=limiter_key,
                        failure_count=failure_count,
                        window_started_at=window_started_at,
                        locked_until=locked_until,
                        updated_at=now,
                    )
            return False, lockout_transition

        lockout_transition = any(item[5] for item in computed)
        with conn.cursor() as cur:
            for (
                limiter_key,
                failure_count,
                window_started_at,
                locked_until,
                _admitted,
                _transitioned,
            ) in computed:
                _upsert_admin_login_limiter_row(
                    cur,
                    limiter_key=limiter_key,
                    failure_count=failure_count,
                    window_started_at=window_started_at,
                    locked_until=locked_until,
                    updated_at=now,
                )
    return True, lockout_transition


def release_admin_login_admission(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
    rate_limit: int,
) -> None:
    """Release one admitted verification slot after successful login."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (limiter_key,))
            cur.execute(
                """
                SELECT failure_count, window_started_at, locked_until
                FROM admin_login_rate_limits
                WHERE limiter_key = %s
                FOR UPDATE
                """,
                (limiter_key,),
            )
            row = cur.fetchone()
            if row is None or int(row["failure_count"]) <= 0:
                return
            failure_count = int(row["failure_count"]) - 1
            locked_until = row["locked_until"]
            if locked_until is not None:
                if locked_until.tzinfo is None:
                    locked_until = locked_until.replace(tzinfo=timezone.utc)
                if failure_count < rate_limit:
                    locked_until = None
            _upsert_admin_login_limiter_row(
                cur,
                limiter_key=limiter_key,
                failure_count=failure_count,
                window_started_at=row["window_started_at"],
                locked_until=locked_until,
                updated_at=now,
            )


def record_admin_login_failure(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> None:
    record_admin_login_failures(
        conn,
        limiter_keys=[limiter_key],
        now=now,
        rate_limit=rate_limit,
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
    )


def record_admin_login_failures(
    conn: psycopg.Connection,
    *,
    limiter_keys: list[str],
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> None:
    unique_keys = sorted(set(limiter_keys))
    if not unique_keys:
        return
    with conn.transaction():
        with conn.cursor() as cur:
            for limiter_key in unique_keys:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (limiter_key,))
                cur.execute(
                    """
                    SELECT failure_count, window_started_at, locked_until
                    FROM admin_login_rate_limits
                    WHERE limiter_key = %s
                    FOR UPDATE
                    """,
                    (limiter_key,),
                )
                row = cur.fetchone()
                failure_count, window_started_at, locked_until, _admitted, _transitioned = (
                    _next_limiter_failure_state(
                        row,
                        now=now,
                        window_seconds=window_seconds,
                        rate_limit=rate_limit,
                        lockout_seconds=lockout_seconds,
                    )
                )
                _upsert_admin_login_limiter_row(
                    cur,
                    limiter_key=limiter_key,
                    failure_count=failure_count,
                    window_started_at=window_started_at,
                    locked_until=locked_until,
                    updated_at=now,
                )


def clear_admin_login_rate_limit(conn: psycopg.Connection, *, limiter_key: str) -> None:
    clear_admin_login_rate_limits(conn, limiter_keys=[limiter_key])


def clear_admin_login_rate_limits(
    conn: psycopg.Connection,
    *,
    limiter_keys: list[str],
) -> None:
    unique_keys = sorted(set(limiter_keys))
    if not unique_keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM admin_login_rate_limits WHERE limiter_key = ANY(%s)",
            (unique_keys,),
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
