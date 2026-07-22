"""Unit tests for CRM backup export and restore verification (#128)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.crm_backup import (
    CRM_BACKUP_TABLES,
    MANIFEST_VERSION,
    build_configuration_block,
    build_snapshot,
    compare_table_counts,
    expected_latest_schema_version,
    validate_snapshot_structure,
    verify_restore,
)


def _mock_conn(
    *,
    table_exists: dict[str, bool] | None = None,
    counts: dict[str, int] | None = None,
    schema_version: str | None = "022",
    migration_name: str = "contact_relationship_metrics",
    distribution_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    exists = table_exists or {table: True for table in CRM_BACKUP_TABLES}
    table_counts = counts or {table: 0 for table in CRM_BACKUP_TABLES}
    dist_rows = distribution_rows or {}

    def execute_side_effect(sql: str, params: tuple[Any, ...] = ()) -> None:
        cur = execute_side_effect.cursor  # type: ignore[attr-defined]
        sql_norm = " ".join(sql.split())
        if "to_regclass" in sql_norm:
            table = str(params[0]).removeprefix("public.")
            cur._result = [{"reg": "public." + table if exists.get(table) else None}]
        elif "COUNT(*)" in sql_norm and "GROUP BY" not in sql_norm:
            for table, count in table_counts.items():
                if f"FROM {table}" in sql_norm:
                    cur._result = [{"n": count}]
                    return
            cur._result = [{"n": 0}]
        elif "GROUP BY" in sql_norm:
            key = next(
                (k for k, rows in dist_rows.items() if k.split(".")[0] in sql_norm),
                None,
            )
            cur._result = dist_rows.get(key, [])
        elif "FROM schema_migrations" in sql_norm and "name" in sql_norm:
            cur._result = (
                [{"name": migration_name}] if schema_version is not None else []
            )
        elif "FROM schema_migrations" in sql_norm:
            cur._result = (
                [{"version": schema_version}] if schema_version is not None else []
            )
        else:
            cur._result = []

    cursor = MagicMock()
    cursor.fetchone.side_effect = lambda: (
        cursor._result[0] if getattr(cursor, "_result", None) else None
    )
    cursor.fetchall.side_effect = lambda: getattr(cursor, "_result", [])
    execute_side_effect.cursor = cursor  # type: ignore[attr-defined]

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    cursor.execute.side_effect = execute_side_effect
    return conn


@pytest.mark.unit
def test_build_configuration_block_tracks_latest_migration() -> None:
    block = build_configuration_block()
    assert block["expected_latest_schema_version"] == expected_latest_schema_version()
    assert block["pipeline_stages"][0] == "researching"
    assert block["analytics_schema_version"] == "1.0.0"


@pytest.mark.unit
def test_build_snapshot_includes_all_tables_and_redaction() -> None:
    counts = {table: 1 for table in CRM_BACKUP_TABLES}
    counts["companies"] = 3
    conn = _mock_conn(
        counts=counts,
        distribution_rows={
            "companies.pipeline_stage": [
                {"bucket": "researching", "n": 2},
                {"bucket": "won", "n": 1},
            ],
        },
    )
    snapshot = build_snapshot(conn)
    assert snapshot["manifest_version"] == MANIFEST_VERSION
    assert snapshot["schema_version"] == "022"
    assert snapshot["table_counts"] == counts
    assert snapshot["distributions"]["companies.pipeline_stage"] == {
        "researching": 2,
        "won": 1,
    }
    assert snapshot["redaction"]["policy"] == "counts_and_aggregates_only"
    assert "email" in snapshot["redaction"]["excluded_row_data"]


@pytest.mark.unit
def test_validate_snapshot_structure_rejects_missing_tables() -> None:
    snapshot = {
        "manifest_version": MANIFEST_VERSION,
        "exported_at": "2026-07-16T00:00:00+00:00",
        "schema_version": "022",
        "table_counts": {"companies": 1},
        "configuration": build_configuration_block(),
    }
    errors = validate_snapshot_structure(snapshot)
    assert any("table_counts missing" in err for err in errors)


@pytest.mark.unit
def test_validate_snapshot_structure_accepts_complete_manifest() -> None:
    conn = _mock_conn(counts={"companies": 2})
    snapshot = build_snapshot(conn)
    assert validate_snapshot_structure(snapshot) == []


@pytest.mark.unit
def test_compare_table_counts_reports_mismatches() -> None:
    expected = {table: 0 for table in CRM_BACKUP_TABLES}
    actual = dict(expected)
    actual["contacts"] = 4
    mismatches = compare_table_counts(actual, expected)
    assert mismatches == ["contacts: expected 0, got 4"]


@pytest.mark.unit
def test_verify_restore_ok_on_matching_snapshot() -> None:
    counts = {table: 0 for table in CRM_BACKUP_TABLES}
    conn = _mock_conn(counts=counts)
    snapshot = build_snapshot(conn)
    with patch("scripts.crm_backup.apply_migrations", return_value=[]):
        result = verify_restore(conn, expected_snapshot=snapshot)
    assert result["ok"] is True
    assert result["migrations_applied"] == []
    assert result["table_counts"] == counts


@pytest.mark.unit
def test_verify_restore_fails_when_migrations_apply() -> None:
    conn = _mock_conn()
    with patch("scripts.crm_backup.apply_migrations", return_value=["019"]):
        result = verify_restore(conn)
    assert result["ok"] is False
    assert any("apply_migrations applied" in err for err in result["errors"])


@pytest.mark.unit
def test_verify_restore_fails_on_count_mismatch() -> None:
    counts = {table: 0 for table in CRM_BACKUP_TABLES}
    conn = _mock_conn(counts=counts)
    snapshot = build_snapshot(conn)
    snapshot["table_counts"]["companies"] = 99
    with patch("scripts.crm_backup.apply_migrations", return_value=[]):
        result = verify_restore(conn, expected_snapshot=snapshot)
    assert result["ok"] is False
    assert result["count_mismatches"]


@pytest.mark.unit
def test_snapshot_json_round_trip_structure() -> None:
    conn = _mock_conn(
        counts={"companies": 5, **{t: 0 for t in CRM_BACKUP_TABLES if t != "companies"}},
        distribution_rows={
            "project_briefs.status": [{"bucket": "paid", "n": 2}],
        },
    )
    snapshot = build_snapshot(conn)
    reloaded = json.loads(json.dumps(snapshot))
    assert validate_snapshot_structure(reloaded) == []
