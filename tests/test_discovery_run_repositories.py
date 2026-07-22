"""Coverage for PostgresDiscoveryRunRepository SQL paths."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.discovery.repository import PostgresDiscoveryRunRepository
from app.discovery.types import DiscoveryCheckpoint

RUN_ID = UUID("cccccccc-cccc-cccc-cccc-ccccccccccc1")


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_create_and_finish() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    created = {"id": RUN_ID, "status": "running"}
    finished = {"id": RUN_ID, "status": "completed"}
    cur.fetchone.side_effect = [created, finished]
    repo = PostgresDiscoveryRunRepository()

    row = repo.create_run(
        conn,
        trigger_type="manual",
        status="running",
        correlation_id="corr-1",
        enabled_sources=["ycombinator"],
        actor="operator",
        lock_acquired=True,
    )
    assert row["id"] == RUN_ID
    assert "INSERT INTO discovery_runs" in cur.execute.call_args.args[0]

    finished_row = repo.finish_run(conn, RUN_ID, status="completed")
    assert finished_row["status"] == "completed"


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_list_page() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"total": 2}
    cur.fetchall.return_value = [{"id": RUN_ID}]
    repo = PostgresDiscoveryRunRepository()
    rows, total = repo.list_page(conn, page=1, per_page=50)
    assert total == 2
    assert len(rows) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_checkpoint_upsert_and_load() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {
            "source_id": "ycombinator",
            "cursor": "3",
            "etag": None,
            "last_modified": None,
            "last_run_at": "2026-07-01T00:00:00+00:00",
        }
    ]
    repo = PostgresDiscoveryRunRepository()
    loaded = repo.load_checkpoints(conn)
    assert loaded["ycombinator"].cursor == "3"

    repo.upsert_checkpoint(
        conn,
        source_id="ycombinator",
        checkpoint=DiscoveryCheckpoint(cursor="4"),
        success=True,
    )
    assert "INSERT INTO discovery_source_checkpoints" in cur.execute.call_args.args[0]
