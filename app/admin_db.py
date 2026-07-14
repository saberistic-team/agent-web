"""Postgres persistence for admin sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg


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
