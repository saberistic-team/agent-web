"""Coverage for PostgresImportBatchRepository SQL paths."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.repositories.postgres import PostgresImportBatchRepository

BATCH_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_create_and_get_by_id() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    created = {
        "id": BATCH_ID,
        "status": "committed",
        "checksum": "abc",
        "summary_counts": {},
    }
    cur.fetchone.return_value = created
    repo = PostgresImportBatchRepository()
    row = repo.create(
        conn,
        source_type="linkedin",
        schema_version="linkedin_export_v1",
        checksum="abc",
        actor="operator",
        status="committed",
        correlation_id="corr-1",
        export_date=date(2026, 1, 15),
        summary_counts={"inserted": 1},
    )
    assert row["id"] == BATCH_ID
    assert "INSERT INTO import_batches" in cur.execute.call_args.args[0]

    cur.fetchone.return_value = created
    assert repo.get_by_id(conn, BATCH_ID)["id"] == BATCH_ID
    assert "WHERE id = %s" in cur.execute.call_args.args[0]


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_get_committed_by_checksum() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": BATCH_ID, "status": "committed"}
    repo = PostgresImportBatchRepository()
    row = repo.get_committed_by_checksum(conn, "checksum-1")
    assert row is not None
    sql = cur.execute.call_args.args[0]
    assert "status = 'committed'" in sql
    assert cur.execute.call_args.args[1] == ("checksum-1",)


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_list_page_clamps_pagination() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"total": 3}
    cur.fetchall.return_value = [{"id": BATCH_ID}]
    repo = PostgresImportBatchRepository()
    rows, total = repo.list_page(conn, page=0, per_page=500)
    assert total == 3
    assert len(rows) == 1
    assert cur.execute.call_args.args[1] == (100, 0)


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_update_status_with_counts_and_error() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": BATCH_ID, "status": "failed"}
    repo = PostgresImportBatchRepository()
    updated = repo.update_status(
        conn,
        BATCH_ID,
        status="failed",
        summary_counts={"skipped": 1},
        error_message="boom",
    )
    assert updated["status"] == "failed"
    sql = cur.execute.call_args.args[0]
    assert "summary_counts = %s::jsonb" in sql
    assert "error_message = %s" in sql


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_update_status_minimal_and_list_all_rows() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": BATCH_ID, "status": "rolled_back"}
    repo = PostgresImportBatchRepository()
    updated = repo.update_status(conn, BATCH_ID, status="rolled_back")
    assert updated["status"] == "rolled_back"
    sql = cur.execute.call_args.args[0]
    assert "summary_counts" not in sql
    assert "error_message" not in sql

    cur.fetchall.return_value = [{"row_index": 0}]
    listed = repo.list_rows_for_batch(conn, BATCH_ID)
    assert listed[0]["row_index"] == 0
    assert "outcome = %s" not in cur.execute.call_args.args[0]
    assert cur.execute.call_args.args[1] == [BATCH_ID, 500]


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_create_row_and_list_rows() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": "row-1", "outcome": "inserted"}
    repo = PostgresImportBatchRepository()
    row = repo.create_row(
        conn,
        batch_id=BATCH_ID,
        row_index=0,
        source_kind="linkedin_connection",
        source_identity={"profile_url": "https://linkedin.com/in/ada"},
        outcome="inserted",
        entity_type="contact",
        entity_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        prior_snapshot=None,
        applied_snapshot={"full_name": "Ada"},
        detail=None,
    )
    assert row["outcome"] == "inserted"
    assert "INSERT INTO import_batch_rows" in cur.execute.call_args.args[0]

    cur.fetchall.return_value = [{"row_index": 0, "outcome": "inserted"}]
    listed = repo.list_rows_for_batch(conn, BATCH_ID, outcome="inserted", limit=10)
    assert listed[0]["outcome"] == "inserted"
    sql = cur.execute.call_args.args[0]
    assert "outcome = %s" in sql
    assert cur.execute.call_args.args[1] == [BATCH_ID, "inserted", 10]


@pytest.mark.unit
@pytest.mark.integration
def test_default_repositories_includes_import_batches() -> None:
    from app.repositories.postgres import default_repositories

    repos = default_repositories()
    assert "import_batches" in repos
    assert isinstance(repos["import_batches"], PostgresImportBatchRepository)
