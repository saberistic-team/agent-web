"""Documentation drift checks for docs/CRM_SCHEMA.md (#277)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest

from app.migrations.definitions import MIGRATIONS, Migration
from app.migrations.runner import apply_migrations
from scripts.check_crm_schema_doc import (
    CANONICAL_PIPELINE_COLUMNS,
    LEGACY_PIPELINE_COLUMNS,
    PROJECT_BRIEF_PAYMENT_COLUMNS,
    validate_crm_schema_doc,
    validate_migration_ledger,
)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres migration tests")


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
            return tuple(selected)
    raise AssertionError(f"migration {version} not found")


def _column_names(conn: psycopg.Connection, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        return {str(row[0]) for row in cur.fetchall()}


@pytest.mark.unit
def test_crm_schema_doc_matches_canonical_migrations() -> None:
    errors = validate_crm_schema_doc()
    assert errors == [], "\n".join(errors)


@pytest.mark.unit
def test_migration_ledger_covers_001_through_016_without_gaps() -> None:
    from scripts.check_crm_schema_doc import load_crm_schema_doc

    errors = validate_migration_ledger(load_crm_schema_doc())
    assert errors == [], "\n".join(errors)
    expected = [m.version for m in MIGRATIONS if m.version <= "016"]
    assert expected == [
        "001",
        "002",
        "003",
        "004",
        "005",
        "006",
        "007",
        "008",
        "009",
        "010",
        "011",
        "012",
        "013",
        "014",
        "015",
        "016",
    ]


@pytest.mark.unit
def test_crm_schema_doc_rejects_legacy_pipeline_column_in_companies_table(
    tmp_path: Any,
) -> None:
    from scripts.check_crm_schema_doc import CRM_SCHEMA_PATH, load_crm_schema_doc

    text = load_crm_schema_doc(CRM_SCHEMA_PATH)
    broken = text.replace(
        "| `pipeline_owner` | `TEXT` | yes | — | Assigned operator",
        "| `owner` | `TEXT` | yes | — | Assigned operator",
        1,
    )
    path = tmp_path / "CRM_SCHEMA.md"
    path.write_text(broken, encoding="utf-8")
    errors = validate_crm_schema_doc(path)
    assert any("legacy pipeline column `owner`" in error for error in errors)


@pytest.mark.integration
def test_post_016_database_columns_match_documented_pipeline_set(
    pg_conn: psycopg.Connection,
) -> None:
    applied = apply_migrations(pg_conn, migrations=_migrations_through("016"))
    assert applied[-1] == "016"

    company_cols = _column_names(pg_conn, "companies")
    for column in CANONICAL_PIPELINE_COLUMNS:
        assert column in company_cols
    for column in LEGACY_PIPELINE_COLUMNS:
        assert column not in company_cols

    brief_cols = _column_names(pg_conn, "project_briefs")
    for column in PROJECT_BRIEF_PAYMENT_COLUMNS:
        assert column in brief_cols


@pytest.mark.integration
def test_legacy_013_upgrade_matches_documented_canonical_columns(
    pg_conn: psycopg.Connection,
) -> None:
    from tests.test_pipeline_schema_reconcile import LEGACY_013_SQL

    apply_migrations(pg_conn, migrations=_migrations_through("012"))
    pg_conn.execute(LEGACY_013_SQL)
    pg_conn.execute(
        """
        INSERT INTO schema_migrations (version, name)
        VALUES ('013', 'acquisition_pipeline')
        ON CONFLICT (version) DO NOTHING
        """
    )
    apply_migrations(pg_conn, migrations=_migrations_through("016"))

    company_cols = _column_names(pg_conn, "companies")
    for column in CANONICAL_PIPELINE_COLUMNS:
        assert column in company_cols
    for column in LEGACY_PIPELINE_COLUMNS:
        assert column in company_cols
