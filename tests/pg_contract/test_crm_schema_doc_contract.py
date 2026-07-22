"""Live-PostgreSQL checks that docs/CRM_SCHEMA.md matches migrations 001–016 (#277)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from app.crm_schema_doc_contract import (
    CANONICAL_COMPANY_PIPELINE_COLUMNS,
    COMPANIES_COLUMNS_THROUGH_016,
    LEGACY_COMPANY_PIPELINE_COLUMNS,
    PROJECT_BRIEF_PAYMENT_COLUMNS,
    migrations_through,
)
from app.migrations.runner import apply_migrations

pytestmark = pytest.mark.contract


def test_fresh_migrations_through_016_match_documented_companies_columns(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    applied = apply_migrations(pg_conn, migrations_through("016"))
    assert applied[-1] == "016"

    columns = db.column_names(pg_conn, "companies")
    assert CANONICAL_COMPANY_PIPELINE_COLUMNS <= columns
    assert columns >= COMPANIES_COLUMNS_THROUGH_016
    assert LEGACY_COMPANY_PIPELINE_COLUMNS.isdisjoint(columns)

    brief_columns = db.column_names(pg_conn, "project_briefs")
    assert PROJECT_BRIEF_PAYMENT_COLUMNS <= brief_columns


def test_legacy_013_upgrade_through_016_matches_fresh_canonical_columns(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn, migrations_through("012"))
    pg_conn.execute(db.LEGACY_013_SQL)
    pg_conn.execute(
        """
        INSERT INTO schema_migrations (version, name)
        VALUES ('013', 'acquisition_pipeline')
        ON CONFLICT (version) DO NOTHING
        """
    )
    company_id = uuid4()
    pg_conn.execute(
        """
        INSERT INTO companies (id, name, status, pipeline_stage, owner, expected_value, stage_reason)
        VALUES (%s, 'Legacy Co', 'prospect', 'lost', 'Alex Owner', %s, 'budget cut')
        """,
        (company_id, Decimal("2500.50")),
    )
    pg_conn.commit()

    upgraded = apply_migrations(pg_conn, migrations_through("016"))
    assert "015" in upgraded
    assert upgraded[-1] == "016"

    columns = db.column_names(pg_conn, "companies")
    assert CANONICAL_COMPANY_PIPELINE_COLUMNS <= columns
    assert columns >= COMPANIES_COLUMNS_THROUGH_016
    assert LEGACY_COMPANY_PIPELINE_COLUMNS <= columns

    company = db.fetch_dict(
        pg_conn, "SELECT * FROM companies WHERE id = %s", (company_id,)
    )
    assert company is not None
    assert company["pipeline_owner"] == "Alex Owner"
    assert company["expected_value_cents"] == 250_050
    assert company["pipeline_loss_reason"] == "budget cut"
    assert company["owner"] == "Alex Owner"

    assert db.table_exists(pg_conn, "pipeline_stage_history")
    assert db.table_exists(pg_conn, "company_stage_history")


def test_legacy_upgrade_preserves_fresh_install_company_column_set(
    pg_conn: psycopg.Connection,
    db: SimpleNamespace,
    database_url: str,
) -> None:
    apply_migrations(pg_conn, migrations_through("012"))
    pg_conn.execute(db.LEGACY_013_SQL)
    pg_conn.execute(
        """
        INSERT INTO schema_migrations (version, name)
        VALUES ('013', 'acquisition_pipeline')
        ON CONFLICT (version) DO NOTHING
        """
    )
    pg_conn.commit()
    apply_migrations(pg_conn, migrations_through("016"))
    pg_conn.commit()
    legacy_columns = _column_types(pg_conn)

    pg_conn.rollback()
    fresh_conn = psycopg.connect(database_url, autocommit=False)
    try:
        fresh_conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        fresh_conn.execute("CREATE SCHEMA public")
        fresh_conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        fresh_conn.execute("GRANT ALL ON SCHEMA public TO public")
        fresh_conn.commit()
        apply_migrations(fresh_conn, migrations_through("016"))
        fresh_conn.commit()
        fresh_columns = _column_types(fresh_conn)
    finally:
        fresh_conn.close()

    for key, data_type in fresh_columns.items():
        if not key.startswith("companies."):
            continue
        assert key in legacy_columns, f"legacy upgrade missing column {key}"
        assert legacy_columns[key] == data_type, f"type drift for {key}"


def _column_types(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, column_name
            """
        )
        rows = cur.fetchall()
    return {f"{table}.{column}": str(data_type) for table, column, data_type in rows}
