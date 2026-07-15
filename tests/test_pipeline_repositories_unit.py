"""Unit tests for pipeline repository SQL."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.repositories.postgres import PostgresPipelineRepository

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.mark.unit
@pytest.mark.integration
def test_list_pipeline_companies_filters_stage() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    repo = PostgresPipelineRepository()
    repo.list_companies(conn, pipeline_stage="qualified", limit=25)
    sql = cur.execute.call_args.args[0]
    params = cur.execute.call_args.args[1]
    assert "pipeline_stage IS NOT NULL" in sql
    assert "pipeline_stage = %s" in sql
    assert params[-2:] == ["qualified", 25]


@pytest.mark.unit
def test_record_stage_history_inserts_row() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": "hist-1", "to_stage": "qualified"}
    repo = PostgresPipelineRepository()
    row = repo.record_stage_history(
        conn,
        company_id=COMPANY_ID,
        from_stage="researching",
        to_stage="qualified",
        changed_by="operator",
    )
    assert row["to_stage"] == "qualified"
    assert "INSERT INTO pipeline_stage_history" in cur.execute.call_args.args[0]


@pytest.mark.unit
def test_overdue_next_actions_query_is_bounded() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    repo = PostgresPipelineRepository()
    repo.list_overdue_next_actions(conn, reference=NOW, limit=10)
    sql = cur.execute.call_args.args[0]
    assert "next_action_due_at < %s" in sql
    assert cur.execute.call_args.args[1] == (NOW, 10)


@pytest.mark.unit
def test_upcoming_next_actions_query_window() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    repo = PostgresPipelineRepository()
    window_end = NOW.replace(day=28)
    repo.list_upcoming_next_actions(
        conn, reference=NOW, window_end=window_end, limit=5
    )
    params = cur.execute.call_args.args[1]
    assert params == (NOW, window_end, 5)


@pytest.mark.unit
def test_update_pipeline_fields_and_history_sql() -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"id": COMPANY_ID, "pipeline_stage": "qualified"}
    repo = PostgresPipelineRepository()
    repo.update_pipeline_fields(
        conn,
        COMPANY_ID,
        next_action="Call",
        clear_loss_reason=True,
    )
    sql = cur.execute.call_args.args[0]
    assert "pipeline_loss_reason = NULL" in sql

    cur.fetchall.return_value = [{"to_stage": "qualified"}]
    history = repo.list_stage_history(conn, COMPANY_ID, limit=5)
    assert history[0]["to_stage"] == "qualified"
