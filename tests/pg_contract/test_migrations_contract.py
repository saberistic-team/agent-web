"""Migration contracts executed by a real PostgreSQL engine (#228).

Covers fresh application from an empty database, schema expectations, idempotent
re-application, digest/version immutability guards, the supported pre-#210
legacy-upgrade fixture, and advisory-lock serialization of concurrent startups.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import tuple_row

from app.migrations.definitions import (
    FROZEN_MIGRATION_DIGESTS,
    MIGRATIONS,
    Migration,
    migration_content_digest,
)
from app.migrations.runner import apply_migrations

ALL_VERSIONS = [m.version for m in MIGRATIONS]
CORE_TABLES = (
    "schema_migrations",
    "project_briefs",
    "companies",
    "contacts",
    "source_records",
    "activities",
    "admin_users",
    "admin_sessions",
    "admin_login_rate_limits",
    "admin_login_flows",
    "audit_events",
    "research_records",
    "pipeline_stage_history",
    "import_batches",
    "import_batch_rows",
    "analytics_events",
    "analytics_sessions",
    "analytics_event_rate_limits",
)


def _migrations_through(version: str) -> tuple[Migration, ...]:
    selected: list[Migration] = []
    for migration in MIGRATIONS:
        selected.append(migration)
        if migration.version == version:
            return tuple(selected)
    raise AssertionError(f"migration {version} not found")


def test_fresh_database_applies_every_migration_in_order(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    applied = apply_migrations(pg_conn)
    assert applied == ALL_VERSIONS

    recorded = db.fetch_dicts(
        pg_conn, "SELECT version, name FROM schema_migrations ORDER BY version"
    )
    assert [str(row["version"]) for row in recorded] == ALL_VERSIONS
    assert [str(row["name"]) for row in recorded] == [m.name for m in MIGRATIONS]


def test_reapply_is_noop_and_structurally_stable(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    assert apply_migrations(pg_conn) == ALL_VERSIONS
    before = db.structure_signature(pg_conn)
    assert apply_migrations(pg_conn) == []
    assert db.structure_signature(pg_conn) == before


def test_expected_tables_and_columns_exist(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn)
    for table in CORE_TABLES:
        assert db.table_exists(pg_conn, table), f"missing table {table}"

    company_cols = db.column_names(pg_conn, "companies")
    for col in (
        "pipeline_stage",
        "next_action",
        "next_action_due_at",
        "pipeline_owner",
        "expected_value_cents",
        "pipeline_loss_reason",
        "pipeline_nurture_reason",
        "domain",
        "archived_at",
    ):
        assert col in company_cols, f"companies missing {col}"

    contact_cols = db.column_names(pg_conn, "contacts")
    for col in ("email", "profile_url", "buying_roles", "archived_at"):
        assert col in contact_cols, f"contacts missing {col}"


def test_partial_active_email_unique_index_is_defined(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn)
    definition = db.index_def(pg_conn, "idx_contacts_email_unique")
    assert definition is not None
    assert "UNIQUE" in definition
    assert "lower(email)" in definition.lower()
    assert "archived_at IS NULL" in definition


def test_source_and_import_uniqueness_constraints(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn)
    source_def = db.constraint_def(pg_conn, "source_records_type_external_unique")
    assert source_def is not None
    assert "UNIQUE" in source_def
    assert "source_type" in source_def and "external_id" in source_def

    committed_idx = db.index_def(pg_conn, "idx_import_batches_checksum_committed")
    assert committed_idx is not None
    assert "UNIQUE" in committed_idx
    assert "status = 'committed'" in committed_idx


def test_pipeline_stage_check_constraint_matches_stage_keys(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn)
    definition = db.constraint_def(pg_conn, "companies_pipeline_stage_check")
    assert definition is not None
    for stage in ("researching", "diagnostic_paid", "won", "lost", "nurture"):
        assert stage in definition


def test_audit_events_have_append_only_triggers(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    apply_migrations(pg_conn)
    triggers = db.trigger_names(pg_conn, "audit_events")
    assert "audit_events_no_update" in triggers
    assert "audit_events_no_delete" in triggers


def test_audit_events_reject_update_and_delete(
    pg_conn: psycopg.Connection,
) -> None:
    apply_migrations(pg_conn)
    pg_conn.commit()
    pg_conn.execute(
        """
        INSERT INTO audit_events (actor, action, correlation_id)
        VALUES ('operator', 'test.event', 'corr-immutable')
        """
    )
    pg_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        pg_conn.execute("UPDATE audit_events SET action = 'tampered'")
    pg_conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        pg_conn.execute("DELETE FROM audit_events")
    pg_conn.rollback()


def test_applied_versions_cannot_be_silently_redefined(
    pg_conn: psycopg.Connection, db: SimpleNamespace
) -> None:
    """A recorded version is never re-executed even if its SQL is later edited."""
    apply_migrations(pg_conn)
    before = db.structure_signature(pg_conn)

    tampered = tuple(
        Migration(
            version=m.version,
            name=m.name,
            up_sql=(m.up_sql + "\nALTER TABLE companies ADD COLUMN tampered_col TEXT;")
            if m.version == "003"
            else m.up_sql,
        )
        for m in MIGRATIONS
    )
    # Version 003 is already recorded, so its (mutated) SQL must not run.
    assert apply_migrations(pg_conn, tampered) == []
    assert db.structure_signature(pg_conn) == before
    assert "tampered_col" not in db.column_names(pg_conn, "companies")

    original = next(m for m in MIGRATIONS if m.version == "003")
    mutated = next(m for m in tampered if m.version == "003")
    # The content digest changes, so a real source edit is reviewable/detectable.
    assert migration_content_digest(original) != migration_content_digest(mutated)


def test_frozen_digests_match_and_form_contiguous_prefix() -> None:
    by_version = {m.version: m for m in MIGRATIONS}
    frozen = FROZEN_MIGRATION_DIGESTS
    assert frozen
    assert set(frozen).issubset(by_version)

    frozen_in_order = [v for v in ALL_VERSIONS if v in frozen]
    assert frozen_in_order == ALL_VERSIONS[: len(frozen_in_order)]
    for version, expected in frozen.items():
        assert migration_content_digest(by_version[version]) == expected

    digests = [migration_content_digest(m) for m in MIGRATIONS]
    assert len(set(digests)) == len(digests)
    assert len(set(ALL_VERSIONS)) == len(ALL_VERSIONS)


def test_legacy_pre_reconciliation_fixture_upgrades_and_preserves_data(
    pg_conn: psycopg.Connection, db: SimpleNamespace, database_url: str
) -> None:
    # Bring the database to the pre-#210 legacy state: 001–012 canonical, then
    # the earlier incompatible form of 013 (owner/expected_value/stage_reason +
    # company_stage_history), recorded as version 013.
    apply_migrations(pg_conn, _migrations_through("012"))
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
    pg_conn.execute(
        """
        INSERT INTO company_stage_history (
            company_id, from_stage, to_stage, changed_by, reason, metadata
        )
        VALUES (%s, 'qualified', 'lost', 'operator', 'budget cut', %s::jsonb)
        """,
        (company_id, "{}"),
    )
    pg_conn.commit()

    assert "pipeline_owner" not in db.column_names(pg_conn, "companies")

    upgraded = apply_migrations(pg_conn)
    assert upgraded == ALL_VERSIONS[ALL_VERSIONS.index("013") + 1:]

    company = db.fetch_dict(
        pg_conn, "SELECT * FROM companies WHERE id = %s", (company_id,)
    )
    assert company is not None
    assert company["pipeline_owner"] == "Alex Owner"
    assert company["expected_value_cents"] == 250_050
    assert company["pipeline_loss_reason"] == "budget cut"
    assert company["owner"] == "Alex Owner"

    assert db.table_exists(pg_conn, "pipeline_stage_history")
    history = db.fetch_dict(
        pg_conn,
        "SELECT * FROM pipeline_stage_history WHERE company_id = %s",
        (company_id,),
    )
    assert history is not None
    assert history["from_stage"] == "qualified"
    assert history["to_stage"] == "lost"
    assert history["metadata"] == {"legacy_reason": "budget cut"}

    # The canonical (fresh-install) schema must be fully realized after the
    # legacy upgrade. Legacy-only artifacts (owner, expected_value, stage_reason,
    # company_stage_history) are intentionally retained per migration 015, so the
    # fresh shape must be a *subset* of the legacy-upgraded shape.
    legacy_cols, legacy_idx = _schema_shape(pg_conn)
    # Release any AccessShareLocks from the reads above so the fresh-install
    # rebuild (on the same database) does not block on this connection.
    pg_conn.rollback()
    fresh_cols, fresh_idx = _fresh_schema_shape(database_url)

    for key, data_type in fresh_cols.items():
        assert key in legacy_cols, f"legacy upgrade missing column {key}"
        assert legacy_cols[key] == data_type, f"type drift for {key}"
    for name, definition in fresh_idx.items():
        assert name in legacy_idx, f"legacy upgrade missing index {name}"
        assert legacy_idx[name] == definition, f"index drift for {name}"


def _schema_shape(
    conn: psycopg.Connection,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({"table.column": data_type}, {index_name: indexdef}) for ``public``."""
    columns: dict[str, str] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            """
        )
        for table_name, column_name, data_type in cur.fetchall():
            columns[f"{table_name}.{column_name}"] = str(data_type)
    indexes: dict[str, str] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
        )
        for indexname, indexdef in cur.fetchall():
            indexes[str(indexname)] = str(indexdef)
    return columns, indexes


def _fresh_schema_shape(dsn: str) -> tuple[dict[str, str], dict[str, str]]:
    conn = psycopg.connect(dsn, autocommit=False, row_factory=tuple_row)
    try:
        conn.rollback()
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        apply_migrations(conn)
        conn.commit()
        return _schema_shape(conn)
    finally:
        conn.close()


def test_concurrent_migration_runs_are_serialized_by_advisory_lock(
    pg_conn: psycopg.Connection,
    db: SimpleNamespace,
    connect: Callable[..., psycopg.Connection],
) -> None:
    """Two startups racing on an empty DB apply each version exactly once.

    Exercises the production migration advisory lock (``pg_try_advisory_xact_lock``
    in ``app.migrations.runner``) with genuinely separate connections.
    """
    barrier = threading.Barrier(2)
    applied: list[list[str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run() -> None:
        conn = connect(row_factory=tuple_row)
        try:
            barrier.wait(timeout=10)
            result = apply_migrations(conn)
            with lock:
                applied.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    # Every version is applied exactly once across the two racing runs.
    combined = sorted(v for run_versions in applied for v in run_versions)
    assert combined == sorted(ALL_VERSIONS)

    recorded = db.fetch_dicts(
        pg_conn, "SELECT version FROM schema_migrations ORDER BY version"
    )
    assert [str(row["version"]) for row in recorded] == ALL_VERSIONS
