"""Documentation/schema drift checks for docs/CRM_SCHEMA.md (#277)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from app.migrations.definitions import MIGRATIONS, Migration
from app.migrations.runner import apply_migrations
from scripts.check_crm_schema_docs import (
    CANONICAL_PIPELINE_COLUMNS,
    LEGACY_PIPELINE_COLUMNS,
    PAYMENT_DETAIL_COLUMNS,
    check_crm_schema_docs,
)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres schema tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
    with _connect(database_url) as conn:
        _reset_public_schema(conn)
        try:
            yield conn
        finally:
            conn.rollback()
            _reset_public_schema(conn)


def _migrations_through(version: str) -> tuple[Migration, ...]:
    selected: list[Migration] = []
    for migration in MIGRATIONS:
        selected.append(migration)
        if migration.version == version:
            break
    else:
        raise AssertionError(f"migration {version} not found")
    return tuple(selected)


def _company_columns(conn: psycopg.Connection) -> set[str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'companies'
            """
        )
        return {str(row["column_name"]) for row in cur.fetchall()}


def _brief_columns(conn: psycopg.Connection) -> set[str]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'project_briefs'
            """
        )
        return {str(row["column_name"]) for row in cur.fetchall()}


@pytest.mark.unit
def test_crm_schema_docs_passes() -> None:
    result = check_crm_schema_docs()
    assert result.ok, "\n".join(result.errors)


@pytest.mark.unit
def test_crm_schema_docs_flags_missing_migration(tmp_path: Any) -> None:
    schema = tmp_path / "CRM_SCHEMA.md"
    schema.write_text("# stub\n", encoding="utf-8")
    result = check_crm_schema_docs(schema_path=schema)
    assert not result.ok
    assert any("migration ledger missing" in err for err in result.errors)


@pytest.mark.integration
def test_postgres_schema_through_016_matches_canonical_pipeline_columns(
    pg_conn: psycopg.Connection,
) -> None:
    applied = apply_migrations(pg_conn, migrations=_migrations_through("016"))
    assert applied[-1] == "016"

    columns = _company_columns(pg_conn)
    assert CANONICAL_PIPELINE_COLUMNS.issubset(columns)
    assert LEGACY_PIPELINE_COLUMNS.isdisjoint(columns)

    brief_columns = _brief_columns(pg_conn)
    assert PAYMENT_DETAIL_COLUMNS.issubset(brief_columns)
