"""Unit tests for CRM migrations (no live Postgres)."""

from __future__ import annotations

import threading
from collections import defaultdict
from unittest.mock import MagicMock

import psycopg
import pytest

from app.migrations.definitions import MIGRATIONS, Migration
from app.migrations.runner import (
    ADVISORY_LOCK_SQL,
    MIGRATION_ADVISORY_LOCK_KEY1,
    MIGRATION_ADVISORY_LOCK_KEY2,
    MigrationLockTimeoutError,
    apply_migrations,
    pending_migrations,
)


def _mock_migration_conn(*, applied_rows: list[tuple[str, ...]] | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (True,)
    cur.fetchall.return_value = applied_rows if applied_rows is not None else []
    return conn


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
    assert [m.version for m in pending] == ["003", "004", "005", "006", "007", "008", "009", "010", "011", "012", "013"]


@pytest.mark.unit
def test_apply_migrations_runs_only_pending_steps() -> None:
    conn = _mock_migration_conn(applied_rows=[("001",), ("002",)])
    cur = conn.cursor.return_value.__enter__.return_value

    applied = apply_migrations(conn, migrations=MIGRATIONS)

    assert applied == ["003", "004", "005", "006", "007", "008", "009", "010", "011", "012", "013"]
    execute_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert execute_calls[0] == ADVISORY_LOCK_SQL
    assert cur.execute.call_args_list[0].args[1] == (
        MIGRATION_ADVISORY_LOCK_KEY1,
        MIGRATION_ADVISORY_LOCK_KEY2,
    )
    assert any("schema_migrations" in sql for sql in execute_calls)
    assert any("crm_foundation" not in sql and "companies" in sql for sql in execute_calls)
    assert any("admin_sessions" in sql for sql in execute_calls)
    assert any("admin_login_rate_limits" in sql for sql in execute_calls)
    assert any("admin_login_flows" in sql for sql in execute_calls)
    assert any("research_records" in sql for sql in execute_calls)
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "003" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "004" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "005" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "006" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "008" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    assert any(
        "INSERT INTO schema_migrations" in str(call.args[0]) and "009" in str(call.args[1])
        for call in cur.execute.call_args_list
    )
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_apply_migrations_on_empty_database_applies_all() -> None:
    conn = _mock_migration_conn()

    applied = apply_migrations(conn, migrations=MIGRATIONS)

    assert applied == ["001", "002", "003", "004", "005", "006", "007", "008", "009", "010", "011", "012", "013"]
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_admin_sessions_migration_is_idempotent() -> None:
    sessions = next(m for m in MIGRATIONS if m.name == "admin_sessions")
    assert sessions.version == "004"
    assert "CREATE TABLE IF NOT EXISTS admin_sessions" in sessions.up_sql
    assert "token_hash TEXT NOT NULL UNIQUE" in sessions.up_sql
    assert "revoked_at TIMESTAMPTZ" in sessions.up_sql
    assert "CREATE INDEX IF NOT EXISTS admin_sessions_token_hash_idx" in sessions.up_sql


@pytest.mark.unit
def test_admin_login_rate_limits_migration_is_idempotent() -> None:
    rate_limits = next(m for m in MIGRATIONS if m.name == "admin_login_rate_limits")
    assert rate_limits.version == "005"
    assert "CREATE TABLE IF NOT EXISTS admin_login_rate_limits" in rate_limits.up_sql
    assert "limiter_key TEXT PRIMARY KEY" in rate_limits.up_sql
    assert "locked_until TIMESTAMPTZ" in rate_limits.up_sql
    assert "CREATE INDEX IF NOT EXISTS admin_login_rate_limits_locked_until_idx" in rate_limits.up_sql


@pytest.mark.unit
def test_admin_csrf_binding_migration_is_idempotent() -> None:
    csrf_binding = next(m for m in MIGRATIONS if m.name == "admin_csrf_binding")
    assert csrf_binding.version == "006"
    assert "CREATE TABLE IF NOT EXISTS admin_login_flows" in csrf_binding.up_sql
    assert "flow_token_hash TEXT NOT NULL UNIQUE" in csrf_binding.up_sql
    assert "csrf_token_hash TEXT NOT NULL" in csrf_binding.up_sql
    assert "consumed_at TIMESTAMPTZ" in csrf_binding.up_sql
    assert "ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS csrf_token_hash TEXT" in (
        csrf_binding.up_sql
    )


@pytest.mark.unit
def test_research_records_migration_is_idempotent() -> None:
    research = next(m for m in MIGRATIONS if m.name == "research_records")
    assert research.version == "008"
    assert "CREATE TABLE IF NOT EXISTS research_records" in research.up_sql
    assert "verified_fact" in research.up_sql
    assert "public_signal" in research.up_sql
    assert "hypothesis" in research.up_sql
    assert "source_url TEXT" in research.up_sql
    assert "expires_at TIMESTAMPTZ" in research.up_sql
    assert "idx_research_records_company_id" in research.up_sql


@pytest.mark.unit
def test_admin_login_flows_cleanup_indexes_migration_is_idempotent() -> None:
    cleanup = next(m for m in MIGRATIONS if m.name == "admin_login_flows_cleanup_indexes")
    assert cleanup.version == "009"
    assert "admin_login_flows_expires_at_idx" in cleanup.up_sql
    assert "admin_login_flows_consumed_at_idx" in cleanup.up_sql
    assert "WHERE consumed_at IS NULL" in cleanup.up_sql
    assert "WHERE consumed_at IS NOT NULL" in cleanup.up_sql


@pytest.mark.unit
def test_migration_rollback_strategy_is_forward_only() -> None:
    for migration in MIGRATIONS:
        assert not hasattr(migration, "down_sql")
        assert "IF NOT EXISTS" in migration.up_sql or "ADD COLUMN IF NOT EXISTS" in migration.up_sql

    crm = next(m for m in MIGRATIONS if m.name == "crm_foundation")
    assert isinstance(crm, Migration)
    assert crm.version == "003"


class _SharedMigrationDatabase:
    """Simulates Postgres migration state for concurrent startup tests."""

    def __init__(self) -> None:
        self._advisory_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._applied_versions: set[str] = set()
        self._up_sql_runs: dict[str, int] = defaultdict(int)

    def try_advisory_xact_lock(self) -> bool:
        return self._advisory_lock.acquire(blocking=False)

    def release_advisory_xact_lock(self) -> None:
        if self._advisory_lock.locked():
            self._advisory_lock.release()

    def fetch_applied_versions(self) -> list[tuple[str]]:
        with self._state_lock:
            return [(version,) for version in sorted(self._applied_versions)]

    def run_up_sql(self, version: str, up_sql: str) -> None:
        with self._state_lock:
            self._up_sql_runs[up_sql] += 1
        threading.Event().wait(0.02)

    def record_version(self, version: str) -> None:
        with self._state_lock:
            self._applied_versions.add(version)


def _make_shared_conn(shared_db: _SharedMigrationDatabase) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def execute(sql: str, params: tuple[object, ...] | None = None) -> None:
        if sql == ADVISORY_LOCK_SQL:
            cur._lock_result = (shared_db.try_advisory_xact_lock(),)
            return
        if "SELECT version FROM schema_migrations" in sql:
            cur._fetchall_result = shared_db.fetch_applied_versions()
            return
        if "INSERT INTO schema_migrations" in sql:
            assert params is not None
            shared_db.record_version(str(params[0]))
            return
        for migration in MIGRATIONS:
            if sql == migration.up_sql:
                shared_db.run_up_sql(migration.version, migration.up_sql)
                return

    cur.execute.side_effect = execute
    cur.fetchone.side_effect = lambda: getattr(cur, "_lock_result", (True,))
    cur.fetchall.side_effect = lambda: getattr(cur, "_fetchall_result", [])
    conn.commit.side_effect = shared_db.release_advisory_xact_lock
    conn.rollback.side_effect = shared_db.release_advisory_xact_lock
    return conn


@pytest.mark.unit
def test_concurrent_initializers_apply_each_migration_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_db = _SharedMigrationDatabase()
    errors: list[BaseException] = []

    def run_initializer() -> None:
        try:
            apply_migrations(_make_shared_conn(shared_db), migrations=MIGRATIONS)
        except BaseException as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    monkeypatch.setattr("app.migrations.runner.LOCK_RETRY_INTERVAL_SECONDS", 0.01)

    threads = [threading.Thread(target=run_initializer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert shared_db._applied_versions == {"001", "002", "003", "004", "005", "006", "007", "008", "009", "010", "011", "012", "013"}
    assert all(count == 1 for count in shared_db._up_sql_runs.values())
    assert len(shared_db._up_sql_runs) == len(MIGRATIONS)


@pytest.mark.unit
def test_apply_migrations_rolls_back_and_retries_after_failure() -> None:
    sample_migrations = (
        Migration(version="001", name="one", up_sql="MIGRATION_ONE_SQL"),
        Migration(version="002", name="two", up_sql="MIGRATION_TWO_SQL"),
    )
    conn = _mock_migration_conn()
    cur = conn.cursor.return_value.__enter__.return_value

    def fail_on_second_migration(sql: str, params: tuple[object, ...] | None = None) -> None:
        if sql == "MIGRATION_TWO_SQL":
            raise psycopg.Error("migration failed")

    cur.execute.side_effect = fail_on_second_migration

    with pytest.raises(psycopg.Error, match="migration failed"):
        apply_migrations(conn, migrations=sample_migrations)

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()

    conn.reset_mock()
    cur.reset_mock()
    cur.fetchone.return_value = (True,)
    cur.fetchall.return_value = []
    cur.execute.side_effect = None

    applied = apply_migrations(conn, migrations=sample_migrations)

    assert applied == ["001", "002"]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.unit
def test_apply_migrations_raises_when_lock_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _mock_migration_conn()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (False,)

    monkeypatch.setattr("app.migrations.runner.LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("app.migrations.runner.LOCK_RETRY_INTERVAL_SECONDS", 0.02)

    with pytest.raises(MigrationLockTimeoutError, match="Timed out waiting"):
        apply_migrations(conn, migrations=MIGRATIONS)

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()

@pytest.mark.unit
def test_contact_records_migration_is_idempotent() -> None:
    contacts = next(m for m in MIGRATIONS if m.name == "contact_records")
    assert contacts.version == "012"
    assert "ALTER TABLE contacts ALTER COLUMN email DROP NOT NULL" in contacts.up_sql
    assert "buying_roles TEXT[]" in contacts.up_sql
    assert "idx_contacts_email_unique" in contacts.up_sql
    assert "idx_contacts_buying_roles" in contacts.up_sql


@pytest.mark.unit
def test_audit_events_migration_is_append_only() -> None:
    audit = next(m for m in MIGRATIONS if m.name == "audit_events")
    assert audit.version == "007"
    assert "CREATE TABLE IF NOT EXISTS audit_events" in audit.up_sql
    assert "prevent_audit_events_mutation" in audit.up_sql
    assert "BEFORE UPDATE ON audit_events" in audit.up_sql
    assert "BEFORE DELETE ON audit_events" in audit.up_sql


@pytest.mark.unit
def test_acquisition_pipeline_migration_adds_columns_and_history() -> None:
    pipeline = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    assert pipeline.version == "013"
    assert "pipeline_stage TEXT" in pipeline.up_sql
    assert "pipeline_stage_history" in pipeline.up_sql
    assert "outreach" in pipeline.up_sql
    assert "task_completion" in pipeline.up_sql

