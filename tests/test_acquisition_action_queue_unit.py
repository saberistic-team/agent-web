"""Unit tests for acquisition action queue prioritization and repository queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.acquisition_action_queue import (
    QUEUE_CATEGORY_DUE_TODAY,
    QUEUE_CATEGORY_OVERDUE,
    QUEUE_CATEGORY_STALE_EVIDENCE,
    QUEUE_CATEGORY_TIER_A,
    QUEUE_CATEGORY_WARM_INTRO,
    QUEUE_PRIORITIZATION_RULES,
    ActionQueueItem,
    load_action_queue,
    prioritize_queue_items,
)
from app.repositories.postgres import (
    PostgresActionQueueRepository,
    PostgresPipelineRepository,
)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
RECORD_ID = UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def _mock_conn(rows: list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    return conn


@pytest.mark.unit
def test_queue_rules_are_documented() -> None:
    assert QUEUE_CATEGORY_OVERDUE in QUEUE_PRIORITIZATION_RULES
    assert QUEUE_CATEGORY_DUE_TODAY in QUEUE_PRIORITIZATION_RULES
    assert QUEUE_CATEGORY_TIER_A in QUEUE_PRIORITIZATION_RULES
    assert QUEUE_CATEGORY_WARM_INTRO in QUEUE_PRIORITIZATION_RULES
    assert QUEUE_CATEGORY_STALE_EVIDENCE in QUEUE_PRIORITIZATION_RULES
    assert "UTC" in QUEUE_PRIORITIZATION_RULES[QUEUE_CATEGORY_OVERDUE]
    assert "interim" in QUEUE_PRIORITIZATION_RULES[QUEUE_CATEGORY_TIER_A].lower()


@pytest.mark.unit
def test_prioritize_overdue_before_due_today() -> None:
    overdue = ActionQueueItem(
        item_key="overdue:1",
        priority_rank=1,
        category=QUEUE_CATEGORY_OVERDUE,
        reason="overdue",
        company_id="1",
        company_name="Zebra",
        next_action_due_at=NOW - timedelta(days=2),
    )
    due_today = ActionQueueItem(
        item_key="due_today:2",
        priority_rank=2,
        category=QUEUE_CATEGORY_DUE_TODAY,
        reason="today",
        company_id="2",
        company_name="Alpha",
        next_action_due_at=NOW,
    )
    result = prioritize_queue_items([due_today, overdue], limit=10)
    assert result[0].category == QUEUE_CATEGORY_OVERDUE
    assert result[1].category == QUEUE_CATEGORY_DUE_TODAY


@pytest.mark.unit
def test_prioritize_deduplicates_by_item_key() -> None:
    item_a = ActionQueueItem(
        item_key="overdue:1",
        priority_rank=1,
        category=QUEUE_CATEGORY_OVERDUE,
        reason="first",
        company_id="1",
        company_name="Acme",
        next_action_due_at=NOW - timedelta(days=1),
    )
    item_b = ActionQueueItem(
        item_key="overdue:1",
        priority_rank=1,
        category=QUEUE_CATEGORY_OVERDUE,
        reason="duplicate",
        company_id="1",
        company_name="Acme",
        next_action_due_at=NOW - timedelta(days=2),
    )
    result = prioritize_queue_items([item_a, item_b], limit=10)
    assert len(result) == 1
    # Earlier due_at sorts first among duplicate keys.
    assert result[0].reason == "duplicate"


@pytest.mark.unit
def test_due_today_sql_uses_utc_day_bounds() -> None:
    repo = PostgresPipelineRepository()
    day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    conn = _mock_conn([])
    repo.list_due_today_next_actions(
        conn, day_start=day_start, day_end=day_end, limit=20
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "next_action_due_at >= %s" in sql
    assert "next_action_due_at < %s" in sql
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert params[0] == day_start
    assert params[1] == day_end


@pytest.mark.unit
def test_tier_a_sql_filters_target_and_qualified_history() -> None:
    repo = PostgresActionQueueRepository()
    since = NOW - timedelta(days=14)
    conn = _mock_conn([])
    repo.list_recently_qualified_tier_a(conn, since=since, limit=10)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "target_status = 'target'" in sql
    assert "to_stage = 'qualified'" in sql
    assert "pipeline_stage_history" in sql


@pytest.mark.unit
def test_warm_intro_sql_checks_roles_and_strength() -> None:
    repo = PostgresActionQueueRepository()
    conn = _mock_conn([])
    repo.list_warm_introduction_opportunities(conn, limit=10)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "introducer" in sql
    assert "relationship_strength" in sql


@pytest.mark.unit
def test_stale_evidence_sql_filters_high_value() -> None:
    repo = PostgresActionQueueRepository()
    conn = _mock_conn([])
    repo.list_stale_high_value_evidence(
        conn, reference=NOW, min_value_cents=50_000, limit=10
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "expires_at <= %s" in sql
    assert "expected_value_cents >= %s" in sql
    assert "target_status = 'target'" in sql


@pytest.mark.unit
def test_load_action_queue_composes_categories() -> None:
    repo = MagicMock()
    repo.list_overdue_next_actions.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Overdue Co",
            "pipeline_stage": "qualified",
            "next_action": "Call",
            "next_action_due_at": NOW - timedelta(days=1),
            "pipeline_owner": "alex",
            "expected_value_cents": 100_000,
        }
    ]
    repo.list_due_today_next_actions.return_value = [
        {
            "id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            "name": "Today Co",
            "pipeline_stage": "researching",
            "next_action": "Email",
            "next_action_due_at": NOW.replace(hour=10),
            "pipeline_owner": None,
            "expected_value_cents": None,
        }
    ]
    repo.list_recently_qualified_tier_a.return_value = []
    repo.list_warm_introduction_opportunities.return_value = []
    repo.list_stale_high_value_evidence.return_value = []

    conn = MagicMock()
    data = load_action_queue(conn, repo, now=NOW, limit=20)
    assert len(data.items) == 2
    assert data.items[0].category == QUEUE_CATEGORY_OVERDUE
    assert data.items[1].category == QUEUE_CATEGORY_DUE_TODAY
    assert "Overdue Co" in data.items[0].reason
    assert data.items[0].company_id == str(COMPANY_ID)


@pytest.mark.unit
def test_due_today_excludes_overdue_at_same_reference_day() -> None:
    """Due-today window is [day_start, day_end); overdue is strictly < reference."""
    repo = MagicMock()
    repo.list_overdue_next_actions.return_value = []
    morning = NOW.replace(hour=9, minute=0, second=0, microsecond=0)
    repo.list_due_today_next_actions.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Today Co",
            "pipeline_stage": "qualified",
            "next_action": "Follow up",
            "next_action_due_at": morning,
            "pipeline_owner": "sam",
            "expected_value_cents": 50_000,
        }
    ]
    repo.list_recently_qualified_tier_a.return_value = []
    repo.list_warm_introduction_opportunities.return_value = []
    repo.list_stale_high_value_evidence.return_value = []

    data = load_action_queue(MagicMock(), repo, now=NOW, limit=10)
    assert len(data.items) == 1
    assert data.items[0].category == QUEUE_CATEGORY_DUE_TODAY
    assert "due today" in data.items[0].reason.lower()
