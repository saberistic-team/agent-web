"""Coverage for PostgresDiscoveryRunRepository SQL paths."""

from __future__ import annotations

from datetime import datetime, timezone
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


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_get_by_id_and_sources() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": RUN_ID, "status": "completed"}
    cur.fetchall.return_value = [{"source_id": "ycombinator", "status": "completed"}]
    repo = PostgresDiscoveryRunRepository()

    row = repo.get_by_id(conn, RUN_ID)
    assert row is not None
    assert row["status"] == "completed"

    cur.fetchone.return_value = None
    assert repo.get_by_id(conn, UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")) is None

    cur.fetchall.return_value = [{"source_id": "ycombinator", "status": "completed"}]
    sources = repo.list_sources_for_run(conn, RUN_ID)
    assert len(sources) == 1


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_create_source_result() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")}
    repo = PostgresDiscoveryRunRepository()
    row = repo.create_source_result(
        conn,
        run_id=RUN_ID,
        source_id="ycombinator",
        status="completed",
        fetched_count=10,
        accepted_count=8,
        rejected_count=1,
        error_count=1,
        checkpoint=DiscoveryCheckpoint(cursor="5", etag='W/"etag"'),
        errors=[{"code": "normalize_failed", "message": "bad row"}],
    )
    assert "INSERT INTO discovery_run_sources" in cur.execute.call_args.args[0]
    assert row["id"] == UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_latest_scheduled_started_at() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cur.fetchone.return_value = {"started_at": started}
    repo = PostgresDiscoveryRunRepository()
    assert repo.latest_scheduled_started_at(conn) == started.isoformat()

    cur.fetchone.return_value = None
    assert repo.latest_scheduled_started_at(conn) is None
