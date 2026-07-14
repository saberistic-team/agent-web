"""Unit tests for CRM migrations (no live Postgres)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.migrations.definitions import MIGRATIONS, Migration
from app.migrations.runner import apply_migrations, pending_migrations


@pytest.mark.unit
def test_migrations_are_ordered_and_unique() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


@pytest.mark.unit
def test_crm_migration_uses_uuid_fks_indexes_and_timestamps() -> None:
    crm = next(m for m in MIGRATIONS if m.name == "crm_foundation")
    sql = crm.up_sql

    for table in ("companies", "contacts", "source_records", "activities", "admin_users"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
        assert "UUID PRIMARY KEY DEFAULT gen_random_uuid()" in sql
        assert "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql
        assert "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql

    assert "REFERENCES companies (id)" in sql
    assert "REFERENCES contacts (id)" in sql
    assert "REFERENCES source_records (id)" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts (email)" in sql
    assert "CONSTRAINT contacts_email_unique UNIQUE (email)" in sql
    assert "CONSTRAINT source_records_type_external_unique" in sql
    assert "CONSTRAINT admin_users_email_unique UNIQUE (email)" in sql


@pytest.mark.unit
def test_brief_migrations_remain_idempotent() -> None:
    brief = next(m for m in MIGRATIONS if m.name == "project_briefs")
    utm = next(m for m in MIGRATIONS if m.name == "project_briefs_utm_columns")

    assert "CREATE TABLE IF NOT EXISTS project_briefs" in brief.up_sql
    assert "pending_payment" in brief.up_sql
    assert all("ADD COLUMN IF NOT EXISTS" in utm.up_sql for col in (
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
    ))


@pytest.mark.unit
def test_pending_migrations_skips_applied_versions() -> None:
    applied = {"001", "002"}
    pending = pending_migrations(applied_versions=applied)
    assert [m.version for m in pending] == ["003"]


@pytest.mark.unit
def test_apply_migrations_runs_only_pending_steps() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.side_effect = [
        [("001",), ("002",)],
        [],
    ]

    applied = apply_migrations(conn, migrations=MIGRATIONS)

    assert applied == ["003"]
    execute_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert any("schema_migrations" in sql for sql in execute_calls)
    assert any("crm_foundation" not in sql and "companies" in sql for sql in execute_calls)
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "003" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_apply_migrations_on_empty_database_applies_all() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []

    applied = apply_migrations(conn, migrations=MIGRATIONS)

    assert applied == ["001", "002", "003"]
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_migration_rollback_strategy_is_forward_only() -> None:
    for migration in MIGRATIONS:
        assert not hasattr(migration, "down_sql")
        assert "IF NOT EXISTS" in migration.up_sql or "ADD COLUMN IF NOT EXISTS" in migration.up_sql

    crm = next(m for m in MIGRATIONS if m.name == "crm_foundation")
    assert isinstance(crm, Migration)
    assert crm.version == "003"
