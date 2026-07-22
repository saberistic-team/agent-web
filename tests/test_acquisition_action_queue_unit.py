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


@pytest.mark.unit
def test_load_action_queue_parses_all_categories() -> None:
    repo = MagicMock()
    repo.list_overdue_next_actions.return_value = []
    repo.list_due_today_next_actions.return_value = []
    repo.list_recently_qualified_tier_a.return_value = [
        {
            "id": UUID("44444444-4444-4444-4444-444444444444"),
            "name": "Tier A Co",
            "pipeline_stage": "qualified",
            "pipeline_owner": "alex",
            "expected_value_cents": 200_000,
            "qualified_at": (NOW - timedelta(days=2)).isoformat(),
        }
    ]
    repo.list_warm_introduction_opportunities.return_value = [
        {
            "company_id": UUID("55555555-5555-5555-5555-555555555555"),
            "company_name": "Intro Co",
            "contact_id": CONTACT_ID,
            "contact_name": "Jordan Lee",
            "relationship_strength": "champion",
            "pipeline_stage": "researching",
            "expected_value_cents": 75_000,
        }
    ]
    repo.list_stale_high_value_evidence.return_value = [
        {
            "id": RECORD_ID,
            "company_id": UUID("66666666-6666-6666-6666-666666666666"),
            "company_name": "Evidence Co",
            "confidence": 0.72,
            "source_url": "https://example.com/evidence",
            "pipeline_stage": "qualified",
            "expected_value_cents": 150_000,
        }
    ]

    data = load_action_queue(MagicMock(), repo, now=NOW, limit=10)
    categories = {item.category for item in data.items}
    assert QUEUE_CATEGORY_TIER_A in categories
    assert QUEUE_CATEGORY_WARM_INTRO in categories
    assert QUEUE_CATEGORY_STALE_EVIDENCE in categories
    tier_a = next(i for i in data.items if i.category == QUEUE_CATEGORY_TIER_A)
    assert "Tier A Co" in tier_a.reason
    warm = next(i for i in data.items if i.category == QUEUE_CATEGORY_WARM_INTRO)
    assert warm.contact_id == str(CONTACT_ID)
    assert "Jordan Lee" in warm.reason
    stale = next(i for i in data.items if i.category == QUEUE_CATEGORY_STALE_EVIDENCE)
    assert stale.evidence_confidence == 0.72
    assert "72%" in stale.reason


@pytest.mark.unit
def test_prioritize_respects_limit() -> None:
    items = [
        ActionQueueItem(
            item_key=f"overdue:{index}",
            priority_rank=1,
            category=QUEUE_CATEGORY_OVERDUE,
            reason=f"overdue {index}",
            company_id=str(index),
            company_name=f"Co {index}",
            next_action_due_at=NOW - timedelta(days=index),
        )
        for index in range(5)
    ]
    result = prioritize_queue_items(items, limit=2)
    assert len(result) == 2


@pytest.mark.unit
def test_prioritize_sorts_tier_a_by_recent_qualification() -> None:
    older = ActionQueueItem(
        item_key="tier_a:1",
        priority_rank=3,
        category=QUEUE_CATEGORY_TIER_A,
        reason="older",
        company_id="1",
        company_name="Older Co",
        qualified_at=NOW - timedelta(days=10),
    )
    newer = ActionQueueItem(
        item_key="tier_a:2",
        priority_rank=3,
        category=QUEUE_CATEGORY_TIER_A,
        reason="newer",
        company_id="2",
        company_name="Newer Co",
        qualified_at=NOW - timedelta(days=1),
    )
    result = prioritize_queue_items([older, newer], limit=10)
    assert result[0].company_name == "Newer Co"


@pytest.mark.unit
def test_export_candidates_sql_includes_pipeline_filters() -> None:
    repo = PostgresActionQueueRepository()
    conn = _mock_conn([])
    repo.list_export_candidates(conn, limit=25)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "pipeline_stage NOT IN ('lost', 'nurture')" in sql
    assert "research_records" in sql
