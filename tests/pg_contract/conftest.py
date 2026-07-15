"""Reusable harness for the real-PostgreSQL CRM contract suite (#228).

Every test collected under ``tests/pg_contract/`` runs against a live
PostgreSQL engine. The fixtures here provision an isolated ``public`` schema per
test, apply the versioned migrations from :mod:`app.migrations`, and expose
small SQL/introspection helpers so individual tests stay focused on the
contract they assert.

Selection / isolation:

- Items in this package are auto-marked ``contract`` (see
  ``pytest_collection_modifyitems``) so the fast unit/integration suite and the
  coverage gates in ``scripts/check_coverage.py`` never collect them.
- Without ``TEST_DATABASE_URL`` the suite skips locally; when
  ``REQUIRE_TEST_DATABASE=1`` (CI) a missing URL fails closed instead of
  silently skipping.

Connection row factories: ``apply_migrations`` reads ``cur.fetchone()[0]`` and
therefore needs tuple rows, while the repositories expect ``dict_row``. The
``pg_conn`` fixture yields tuple rows (migration tests) and ``migrated_conn``
yields ``dict_row`` after applying every migration (repository/service tests).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row, tuple_row

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-mark every test in this package ``contract``.

    Keeps the marker authoritative even if a new module forgets to declare
    ``pytestmark``; the fast suite selects ``-m "not contract"`` and the
    coverage gates select ``-m unit`` / ``-m integration``.
    """
    for item in items:
        if "pg_contract" in str(getattr(item, "path", "")):
            item.add_marker(pytest.mark.contract)


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail(
            "REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset — the "
            "PostgreSQL contract suite cannot run. Provision Postgres and set "
            "TEST_DATABASE_URL (see docs/TESTING.md)."
        )
    pytest.skip("TEST_DATABASE_URL not set; skipping live PostgreSQL contract suite")


@pytest.fixture(scope="session")
def database_url() -> str:
    return _require_database_url()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    """Drop and recreate ``public`` so each test starts from an empty database.

    ``apply_migrations`` commits, so a transaction rollback alone cannot undo a
    prior test's DDL — the schema must be physically rebuilt.
    """
    conn.rollback()
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    """Empty database with tuple rows — for migration/DDL contract tests."""
    conn = psycopg.connect(database_url, autocommit=False, row_factory=tuple_row)
    try:
        _reset_public_schema(conn)
        yield conn
    finally:
        try:
            _reset_public_schema(conn)
        finally:
            conn.close()


@pytest.fixture
def migrated_conn(pg_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Fully migrated database with ``dict_row`` — for repository/service tests."""
    from app.migrations.runner import apply_migrations

    apply_migrations(pg_conn)
    pg_conn.commit()
    pg_conn.row_factory = dict_row
    yield pg_conn


@pytest.fixture
def connect(
    database_url: str,
) -> Iterator[Callable[..., psycopg.Connection]]:
    """Factory opening independent connections/transactions (closed on teardown).

    Concurrency contracts require genuinely separate backend connections rather
    than a threaded in-memory simulation, so tests open one connection per
    worker through this factory.
    """
    conns: list[psycopg.Connection] = []

    def _open(*, row_factory: Any = dict_row, autocommit: bool = False) -> psycopg.Connection:
        conn = psycopg.connect(
            database_url, autocommit=autocommit, row_factory=row_factory
        )
        conns.append(conn)
        return conn

    try:
        yield _open
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


# --- SQL / introspection helpers -----------------------------------------


def fetch_dicts(
    conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_dict(
    conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    rows = fetch_dicts(conn, sql, params)
    return rows[0] if rows else None


def count(conn: psycopg.Connection, table: str) -> int:
    row = fetch_dict(conn, f"SELECT COUNT(*) AS n FROM {table}")
    assert row is not None
    return int(row["n"])


def table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = fetch_dict(conn, "SELECT to_regclass(%s) AS reg", (f"public.{name}",))
    assert row is not None
    return row["reg"] is not None


def column_names(conn: psycopg.Connection, table: str) -> set[str]:
    rows = fetch_dicts(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return {str(row["column_name"]) for row in rows}


def index_def(conn: psycopg.Connection, name: str) -> str | None:
    row = fetch_dict(
        conn,
        "SELECT pg_get_indexdef(oid) AS def FROM pg_class WHERE relname = %s",
        (name,),
    )
    return None if row is None else str(row["def"])


def constraint_def(conn: psycopg.Connection, name: str) -> str | None:
    row = fetch_dict(
        conn,
        """
        SELECT pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conname = %s
        """,
        (name,),
    )
    return None if row is None else str(row["def"])


def trigger_names(conn: psycopg.Connection, table: str) -> set[str]:
    rows = fetch_dicts(
        conn,
        """
        SELECT tgname
        FROM pg_trigger
        WHERE tgrelid = %s::regclass AND NOT tgisinternal
        """,
        (f"public.{table}",),
    )
    return {str(row["tgname"]) for row in rows}


def structure_signature(conn: psycopg.Connection) -> str:
    """Digest of tables, columns, and index definitions (schema shape, no data).

    Two databases that reach the same structural signature are interchangeable
    for the application regardless of the migration path taken to get there.
    """
    tables = fetch_dicts(
        conn,
        """
        SELECT table_name,
               string_agg(column_name || ':' || data_type, ',' ORDER BY column_name) AS cols
        FROM information_schema.columns
        WHERE table_schema = 'public'
        GROUP BY table_name
        ORDER BY table_name
        """,
    )
    indexes = fetch_dicts(
        conn,
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY indexname
        """,
    )
    payload = json.dumps(
        {
            "tables": [
                {"table": str(row["table_name"]), "cols": str(row["cols"])}
                for row in tables
            ],
            "indexes": [
                {"name": str(row["indexname"]), "def": str(row["indexdef"])}
                for row in indexes
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def insert_paid_brief(
    conn: psycopg.Connection,
    *,
    website: str = "https://acme.example",
    email: str = "ops@acme.example",
    status: str = "paid",
    brief: str = "Need architecture help with our platform.",
) -> dict[str, Any]:
    """Insert and commit one ``project_briefs`` row; returns the row as a dict."""
    row = fetch_dict(
        conn,
        """
        INSERT INTO project_briefs (website, contact_value, brief, status, utm_source)
        VALUES (%s, %s, %s, %s, 'linkedin')
        RETURNING *
        """,
        (website, email, brief, status),
    )
    assert row is not None
    conn.commit()
    return row


# Earlier incompatible form of migration 013 that some deployed databases
# recorded before #210 reconciled the schema. Used to prove the supported
# legacy-upgrade fixture converges on the canonical schema.
LEGACY_013_SQL = """
ALTER TABLE companies ADD COLUMN IF NOT EXISTS pipeline_stage TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS next_action_due_at TIMESTAMPTZ;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS expected_value NUMERIC(12, 2);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS stage_reason TEXT;

UPDATE companies SET pipeline_stage = 'researching' WHERE pipeline_stage IS NULL;
ALTER TABLE companies ALTER COLUMN pipeline_stage SET DEFAULT 'researching';
ALTER TABLE companies ALTER COLUMN pipeline_stage SET NOT NULL;

ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_pipeline_stage_check;
ALTER TABLE companies ADD CONSTRAINT companies_pipeline_stage_check
    CHECK (pipeline_stage IN (
        'researching', 'qualified', 'ready_for_outreach', 'contacted', 'replied',
        'discovery_scheduled', 'diagnostic_proposed', 'diagnostic_paid',
        'larger_engagement', 'won', 'lost', 'nurture'
    ));

CREATE INDEX IF NOT EXISTS idx_companies_pipeline_stage ON companies (pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_companies_next_action_due_at ON companies (next_action_due_at);
CREATE INDEX IF NOT EXISTS idx_companies_owner ON companies (owner);

CREATE TABLE IF NOT EXISTS company_stage_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by TEXT NOT NULL,
    reason TEXT,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_company_stage_history_company_id
    ON company_stage_history (company_id);

ALTER TABLE activities DROP CONSTRAINT IF EXISTS activities_activity_type_check;
ALTER TABLE activities ADD CONSTRAINT activities_activity_type_check
    CHECK (activity_type IN (
        'note', 'outreach', 'reply', 'meeting', 'proposal', 'payment',
        'task_completion', 'email', 'call', 'status_change'
    ));
"""


@pytest.fixture
def db() -> SimpleNamespace:
    """Bundle of SQL/introspection helpers exposed without fragile imports."""
    return SimpleNamespace(
        fetch_dicts=fetch_dicts,
        fetch_dict=fetch_dict,
        count=count,
        table_exists=table_exists,
        column_names=column_names,
        index_def=index_def,
        constraint_def=constraint_def,
        trigger_names=trigger_names,
        structure_signature=structure_signature,
        insert_paid_brief=insert_paid_brief,
        LEGACY_013_SQL=LEGACY_013_SQL,
    )
