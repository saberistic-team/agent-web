"""Apply versioned migrations and record them in schema_migrations.

Concurrent startup is serialized with a Postgres transaction advisory lock so
only one application instance discovers and applies pending migrations at a time.
See docs/CRM_SCHEMA.md for lock keys and wait behavior.
"""

from __future__ import annotations

import time
from typing import Iterable

import psycopg

from app.migrations.definitions import MIGRATIONS, Migration

SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Stable int4 advisory-lock namespace reserved for agent-web schema migrations.
# key1: 0x41474557 ("AGEW" — agent-web); key2: 0x53434D47 ("SCMG" — schema migrations).
# Unrelated workloads must use different key pairs to avoid accidental collision.
MIGRATION_ADVISORY_LOCK_KEY1 = 0x41474557
MIGRATION_ADVISORY_LOCK_KEY2 = 0x53434D47

ADVISORY_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(%s, %s)"

# Bounded wait when another instance holds the migration lock at startup.
LOCK_RETRY_INTERVAL_SECONDS = 0.25
LOCK_TIMEOUT_SECONDS = 120.0


class MigrationLockTimeoutError(RuntimeError):
    """Another instance held the schema-migration advisory lock past the wait budget."""


def _fetch_applied_versions(cur: psycopg.Cursor) -> set[str]:
    cur.execute("SELECT version FROM schema_migrations")
    return {str(row[0]) for row in cur.fetchall()}


def pending_migrations(
    migrations: Iterable[Migration] = MIGRATIONS,
    applied_versions: set[str] | None = None,
) -> list[Migration]:
    applied = applied_versions or set()
    return [migration for migration in migrations if migration.version not in applied]


def _acquire_migration_lock(cur: psycopg.Cursor) -> None:
    """Acquire the transaction-scoped migration lock, waiting up to LOCK_TIMEOUT_SECONDS."""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        cur.execute(
            ADVISORY_LOCK_SQL,
            (MIGRATION_ADVISORY_LOCK_KEY1, MIGRATION_ADVISORY_LOCK_KEY2),
        )
        acquired = bool(cur.fetchone()[0])
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeoutError(
                "Timed out waiting for the schema-migration advisory lock "
                f"({MIGRATION_ADVISORY_LOCK_KEY1}, {MIGRATION_ADVISORY_LOCK_KEY2}) "
                f"after {LOCK_TIMEOUT_SECONDS:.0f}s. Another application instance is likely "
                "running migrations; retry startup once it finishes or investigate a "
                "stuck migration transaction."
            )
        time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def apply_migrations(conn: psycopg.Connection, migrations: Iterable[Migration] = MIGRATIONS) -> list[str]:
    """Apply pending migrations in order inside one locked transaction.

    Returns versions applied this run. The advisory lock is released on commit,
    rollback, or connection loss.
    """
    applied_now: list[str] = []
    try:
        with conn.cursor() as cur:
            _acquire_migration_lock(cur)
            cur.execute(SCHEMA_MIGRATIONS_SQL)
            applied_versions = _fetch_applied_versions(cur)
            for migration in pending_migrations(migrations, applied_versions):
                cur.execute(migration.up_sql)
                cur.execute(
                    """
                    INSERT INTO schema_migrations (version, name)
                    VALUES (%s, %s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (migration.version, migration.name),
                )
                applied_now.append(migration.version)
                applied_versions.add(migration.version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied_now
