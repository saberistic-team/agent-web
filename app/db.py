"""Render Postgres persistence for project briefs and CRM foundation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Literal

import psycopg
from psycopg.rows import dict_row

from app.migrations.runner import apply_migrations

BriefStatus = Literal["pending_payment", "paid", "abandoned"]


def init_db(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        apply_migrations(conn)


def latest_schema_version(database_url: str) -> str | None:
    """Return the highest applied ``schema_migrations.version``, or None."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version
                FROM schema_migrations
                ORDER BY version DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if row is None:
        return None
    return str(row[0])


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
    payment_subtotal_cents: int | None = None,
    payment_discount_cents: int | None = None,
    payment_amount_cents: int | None = None,
    payment_currency: str | None = None,
    stripe_promotion_code_id: str | None = None,
    stripe_coupon_id: str | None = None,
) -> dict[str, Any] | None:
    paid_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_briefs
            SET status = 'paid',
                stripe_session_id = COALESCE(%s, stripe_session_id),
                stripe_payment_intent_id = %s,
                paid_at = %s,
                payment_subtotal_cents = %s,
                payment_discount_cents = %s,
                payment_amount_cents = %s,
                payment_currency = %s,
                stripe_promotion_code_id = %s,
                stripe_coupon_id = %s
            WHERE id = %s AND status != 'paid'
            RETURNING *
            """,
            (
                stripe_session_id,
                stripe_payment_intent_id,
                paid_at,
                payment_subtotal_cents,
                payment_discount_cents,
                payment_amount_cents,
                payment_currency,
                stripe_promotion_code_id,
                stripe_coupon_id,
                brief_id,
            ),
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


def revoke_admin_session(conn: psycopg.Connection, *, token_hash: str) -> bool:
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
        return cur.rowcount > 0


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
    csrf_token_hash: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Atomically validate and consume a login flow for credential verification.

    Exactly one concurrent caller can claim a matching unconsumed, unexpired row.
    A zero-row update returns ``None`` (failed claim).
    """
    consumed_at = now
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE admin_login_flows
            SET consumed_at = %s
            WHERE flow_token_hash = %s
              AND csrf_token_hash = %s
              AND consumed_at IS NULL
              AND expires_at > %s
            RETURNING id, flow_token_hash, csrf_token_hash, created_at, expires_at, consumed_at
            """,
            (consumed_at, flow_token_hash, csrf_token_hash, now),
        )
        row = cur.fetchone()
        conn.commit()
    return row


def consume_admin_login_flow(
    conn: psycopg.Connection,
    *,
    flow_token_hash: str,
    now: datetime,
) -> bool:
    """Consume an unconsumed flow by cookie identity only (throttle / invalid CSRF).

    Returns ``True`` when a row was updated, ``False`` on a zero-row claim.
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
            RETURNING id
            """,
            (consumed_at, flow_token_hash, now),
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


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


@dataclass(frozen=True)
class AdminLoginAdmission:
    """Result of an atomic shared-store login admission decision."""

    admitted: bool
    throttled: bool
    already_locked: bool
    lockout_transition: bool


def _normalize_limiter_locked_until(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _admin_login_window_expired(
    window_started_at: datetime,
    *,
    now: datetime,
    window_seconds: int,
) -> bool:
    started = window_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started < now - timedelta(seconds=window_seconds)


def reconcile_admin_login_limiter_aliases(
    conn: psycopg.Connection,
    *,
    alias_pairs: tuple[tuple[str, str], ...],
    now: datetime,
    window_seconds: int,
    lockout_seconds: int,
) -> None:
    """Merge previous-key limiter rows into canonical rows during secret rotation."""
    if not alias_pairs:
        return

    with conn.cursor() as cur:
        for canonical_key, legacy_key in alias_pairs:
            if canonical_key == legacy_key:
                continue

            cur.execute(
                """
                SELECT limiter_key, failure_count, window_started_at, locked_until, updated_at
                FROM admin_login_rate_limits
                WHERE limiter_key = %s
                FOR UPDATE
                """,
                (legacy_key,),
            )
            legacy_row = cur.fetchone()
            if legacy_row is None:
                continue

            cur.execute(
                """
                SELECT limiter_key, failure_count, window_started_at, locked_until, updated_at
                FROM admin_login_rate_limits
                WHERE limiter_key = %s
                FOR UPDATE
                """,
                (canonical_key,),
            )
            canonical_row = cur.fetchone()

            legacy_started = legacy_row["window_started_at"]
            if legacy_started.tzinfo is None:
                legacy_started = legacy_started.replace(tzinfo=timezone.utc)
            legacy_locked = _normalize_limiter_locked_until(legacy_row["locked_until"])
            legacy_count = int(legacy_row["failure_count"])

            if canonical_row is None:
                cur.execute(
                    """
                    INSERT INTO admin_login_rate_limits (
                        limiter_key, failure_count, window_started_at, locked_until, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (limiter_key) DO NOTHING
                    """,
                    (
                        canonical_key,
                        legacy_count,
                        legacy_started,
                        legacy_locked,
                        legacy_row["updated_at"],
                    ),
                )
                cur.execute(
                    "DELETE FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (legacy_key,),
                )
                continue

            canonical_started = canonical_row["window_started_at"]
            if canonical_started.tzinfo is None:
                canonical_started = canonical_started.replace(tzinfo=timezone.utc)
            canonical_locked = _normalize_limiter_locked_until(canonical_row["locked_until"])
            canonical_count = int(canonical_row["failure_count"])

            legacy_window_active = not _admin_login_window_expired(
                legacy_started,
                now=now,
                window_seconds=window_seconds,
            )
            canonical_window_active = not _admin_login_window_expired(
                canonical_started,
                now=now,
                window_seconds=window_seconds,
            )

            merged_count = canonical_count
            merged_started = canonical_started
            if legacy_window_active:
                if canonical_window_active:
                    merged_count = max(canonical_count, legacy_count)
                    merged_started = min(canonical_started, legacy_started)
                else:
                    merged_count = legacy_count
                    merged_started = legacy_started

            merged_locked = canonical_locked
            if legacy_locked is not None and legacy_locked > now:
                if merged_locked is None or legacy_locked > merged_locked:
                    merged_locked = legacy_locked

            cur.execute(
                """
                UPDATE admin_login_rate_limits
                SET failure_count = %s,
                    window_started_at = %s,
                    locked_until = %s,
                    updated_at = %s
                WHERE limiter_key = %s
                """,
                (merged_count, merged_started, merged_locked, now, canonical_key),
            )
            cur.execute(
                "DELETE FROM admin_login_rate_limits WHERE limiter_key = %s",
                (legacy_key,),
            )

        conn.commit()


def try_admit_admin_login(
    conn: psycopg.Connection,
    *,
    limiter_keys: tuple[str, ...],
    now: datetime,
    rate_limit: int,
    window_seconds: int,
    lockout_seconds: int,
) -> AdminLoginAdmission:
    """Atomically decide whether a login attempt may reach password verification.

    All ``limiter_keys`` are locked in sorted order inside one transaction so
    concurrent requests cannot overshoot the configured threshold. When any key
    is actively locked, admission is denied without incrementing counters.
    """
    if not limiter_keys:
        return AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )

    ordered_keys = tuple(sorted(limiter_keys))
    with conn.cursor() as cur:
        for limiter_key in ordered_keys:
            cur.execute(
                """
                INSERT INTO admin_login_rate_limits (
                    limiter_key, failure_count, window_started_at, locked_until, updated_at
                )
                VALUES (%s, 0, %s, NULL, %s)
                ON CONFLICT (limiter_key) DO NOTHING
                """,
                (limiter_key, now, now),
            )

        cur.execute(
            """
            SELECT limiter_key, failure_count, window_started_at, locked_until
            FROM admin_login_rate_limits
            WHERE limiter_key = ANY(%s)
            ORDER BY limiter_key
            FOR UPDATE
            """,
            (list(ordered_keys),),
        )
        rows = {str(row["limiter_key"]): row for row in cur.fetchall()}

        for limiter_key in ordered_keys:
            row = rows[limiter_key]
            locked_until = _normalize_limiter_locked_until(row["locked_until"])
            if locked_until is not None and locked_until > now:
                conn.commit()
                return AdminLoginAdmission(
                    admitted=False,
                    throttled=True,
                    already_locked=True,
                    lockout_transition=False,
                )

        updates: dict[str, tuple[int, datetime, datetime | None]] = {}
        lockout_transition = False
        for limiter_key in ordered_keys:
            row = rows[limiter_key]
            window_started_at = row["window_started_at"]
            if window_started_at.tzinfo is None:
                window_started_at = window_started_at.replace(tzinfo=timezone.utc)

            prior_locked_until = _normalize_limiter_locked_until(row["locked_until"])
            if _admin_login_window_expired(
                window_started_at,
                now=now,
                window_seconds=window_seconds,
            ):
                failure_count = 1
                window_started_at = now
            else:
                failure_count = int(row["failure_count"]) + 1

            locked_until: datetime | None = prior_locked_until
            if failure_count >= rate_limit:
                locked_until = now + timedelta(seconds=lockout_seconds)
                if prior_locked_until is None or prior_locked_until <= now:
                    lockout_transition = True

            updates[limiter_key] = (failure_count, window_started_at, locked_until)

        for limiter_key, (failure_count, window_started_at, locked_until) in updates.items():
            cur.execute(
                """
                UPDATE admin_login_rate_limits
                SET failure_count = %s,
                    window_started_at = %s,
                    locked_until = %s,
                    updated_at = %s
                WHERE limiter_key = %s
                """,
                (failure_count, window_started_at, locked_until, now, limiter_key),
            )

        conn.commit()

    return AdminLoginAdmission(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=lockout_transition,
    )


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
    locked_until = _normalize_limiter_locked_until(row["locked_until"])
    if locked_until is None:
        return False
    return locked_until > now


def release_admin_login_admission(
    conn: psycopg.Connection,
    *,
    limiter_key: str,
    now: datetime,
    rate_limit: int,
) -> None:
    """Undo one admitted attempt on success so legitimate logins do not consume failures."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT failure_count, locked_until
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            FOR UPDATE
            """,
            (limiter_key,),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return

        failure_count = max(int(row["failure_count"]) - 1, 0)
        locked_until = _normalize_limiter_locked_until(row["locked_until"])
        if failure_count < rate_limit:
            locked_until = None

        cur.execute(
            """
            UPDATE admin_login_rate_limits
            SET failure_count = %s,
                locked_until = %s,
                updated_at = %s
            WHERE limiter_key = %s
            """,
            (failure_count, locked_until, now, limiter_key),
        )
        conn.commit()


def clear_admin_login_rate_limit(conn: psycopg.Connection, *, limiter_key: str) -> None:
    clear_admin_login_rate_limits(conn, limiter_keys=(limiter_key,))


def clear_admin_login_rate_limits(
    conn: psycopg.Connection,
    *,
    limiter_keys: tuple[str, ...],
) -> None:
    if not limiter_keys:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM admin_login_rate_limits WHERE limiter_key = ANY(%s)",
            (list(limiter_keys),),
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
