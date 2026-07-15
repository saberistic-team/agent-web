"""Postgres advisory lock for brief-to-CRM conversion idempotency."""

from __future__ import annotations

import psycopg

# key1: 0x42524643 ("BRFC" — brief conversion); key2: project_briefs.id (int4).
# Uses a distinct key pair from schema migrations (see app/migrations/runner.py).
BRIEF_CONVERSION_ADVISORY_LOCK_KEY1 = 0x42524643

ADVISORY_XACT_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s, %s)"


def acquire_brief_conversion_lock(conn: psycopg.Connection, brief_id: int) -> None:
    """Block until this transaction holds the brief-scoped advisory lock."""
    with conn.cursor() as cur:
        cur.execute(
            ADVISORY_XACT_LOCK_SQL,
            (BRIEF_CONVERSION_ADVISORY_LOCK_KEY1, brief_id),
        )
