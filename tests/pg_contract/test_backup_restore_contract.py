"""Live PostgreSQL contract tests for CRM backup export and restore (#128)."""

from __future__ import annotations

import json

import psycopg
import pytest

from app.migrations.definitions import MIGRATIONS
from app.migrations.runner import apply_migrations
from scripts.crm_backup import (
    CRM_BACKUP_TABLES,
    build_snapshot,
    validate_snapshot_structure,
    verify_restore,
)


@pytest.mark.contract
def test_export_manifest_structure_on_migrated_database(
    pg_conn: psycopg.Connection,
) -> None:
    apply_migrations(pg_conn)
    pg_conn.commit()

    snapshot = build_snapshot(pg_conn)
    assert validate_snapshot_structure(snapshot) == []
    assert snapshot["schema_version"] == MIGRATIONS[-1].version
    assert set(snapshot["table_counts"]) == set(CRM_BACKUP_TABLES)
    assert snapshot["table_counts"]["schema_migrations"] == len(MIGRATIONS)
    for table in CRM_BACKUP_TABLES:
        if table == "schema_migrations":
            continue
        assert snapshot["table_counts"][table] == 0


@pytest.mark.contract
def test_verify_restore_round_trip_with_export_snapshot(
    pg_conn: psycopg.Connection,
) -> None:
    apply_migrations(pg_conn)
    pg_conn.commit()

    snapshot = build_snapshot(pg_conn)
    reloaded = json.loads(json.dumps(snapshot))

    result = verify_restore(pg_conn, expected_snapshot=reloaded)
    assert result["ok"] is True
    assert result["migrations_applied"] == []
    assert result["table_counts"] == snapshot["table_counts"]


@pytest.mark.contract
def test_verify_restore_detects_count_drift(
    pg_conn: psycopg.Connection,
) -> None:
    apply_migrations(pg_conn)
    pg_conn.commit()

    snapshot = build_snapshot(pg_conn)
    snapshot["table_counts"]["companies"] = 42

    result = verify_restore(pg_conn, expected_snapshot=snapshot)
    assert result["ok"] is False
    assert result["count_mismatches"]
