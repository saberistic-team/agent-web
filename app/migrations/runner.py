"""Apply versioned migrations and record them in schema_migrations."""

from __future__ import annotations

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


def _fetch_applied_versions(cur: psycopg.Cursor) -> set[str]:
    cur.execute("SELECT version FROM schema_migrations")
    return {str(row[0]) for row in cur.fetchall()}


def pending_migrations(
    migrations: Iterable[Migration] = MIGRATIONS,
    applied_versions: set[str] | None = None,
) -> list[Migration]:
    applied = applied_versions or set()
    return [migration for migration in migrations if migration.version not in applied]


def apply_migrations(conn: psycopg.Connection, migrations: Iterable[Migration] = MIGRATIONS) -> list[str]:
    """Apply pending migrations in order. Returns versions applied this run."""
    applied_now: list[str] = []
    with conn.cursor() as cur:
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
    return applied_now
