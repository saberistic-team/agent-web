"""Postgres advisory lock for cross-instance discovery run serialization."""

from __future__ import annotations

import psycopg

# key1: 0x44495343 ("DISC"); key2: 0x52554E21 ("RUN!").
# Distinct from schema migrations and brief conversion locks.
DISCOVERY_RUN_ADVISORY_LOCK_KEY1 = 0x44495343
DISCOVERY_RUN_ADVISORY_LOCK_KEY2 = 0x52554E21

TRY_ADVISORY_LOCK_SQL = "SELECT pg_try_advisory_lock(%s, %s)"
ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s, %s)"


class DiscoveryRunLock:
    """Session-scoped advisory lock held for the duration of a discovery run."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def try_acquire(self) -> bool:
        """Attempt a non-blocking lock; return True when acquired."""
        with self._conn.cursor() as cur:
            cur.execute(
                TRY_ADVISORY_LOCK_SQL,
                (DISCOVERY_RUN_ADVISORY_LOCK_KEY1, DISCOVERY_RUN_ADVISORY_LOCK_KEY2),
            )
            row = cur.fetchone()
            acquired = bool(row["pg_try_advisory_lock"] if isinstance(row, dict) else row[0])
        self._held = acquired
        return acquired

    def release(self) -> None:
        """Release the advisory lock if held."""
        if not self._held:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                ADVISORY_UNLOCK_SQL,
                (DISCOVERY_RUN_ADVISORY_LOCK_KEY1, DISCOVERY_RUN_ADVISORY_LOCK_KEY2),
            )
        self._held = False
