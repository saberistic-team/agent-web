"""Unit tests for acquisition dashboard metrics and repository queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.acquisition_dashboard import (
    METRIC_OVERDUE_NEXT_ACTION,
    METRIC_WITHOUT_DECISION_MAKER,
    AcquisitionDashboardData,
    CountBucket,
    dashboard_is_empty,
    load_acquisition_dashboard,
)
from app.repositories.postgres import PostgresAcquisitionDashboardRepository

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _mock_conn(rows: list | dict | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(rows, list):
        cur.fetchall.return_value = rows
    elif isinstance(rows, dict):
        cur.fetchone.return_value = rows
    return conn


@pytest.mark.unit
def test_metric_definitions_are_explicit() -> None:
    data = AcquisitionDashboardData(
        company_counts_by_stage=(),
        company_counts_by_category=(),
        contact_counts_by_stage=(),
        contact_counts_by_category=(),
        overdue_actions=(),
        upcoming_actions=(),
        recent_evidence=(),
        stale_evidence=(),
        without_decision_maker=(),
        without_next_action=(),
        generated_at=NOW,
    )
    assert "follow_up_note" in data.metric_definitions["overdue_next_action"]
    assert "zero linked contacts" in data.metric_definitions["without_decision_maker"]
    assert METRIC_OVERDUE_NEXT_ACTION == data.metric_definitions["overdue_next_action"]


@pytest.mark.unit
def test_dashboard_is_empty() -> None:
    empty = AcquisitionDashboardData(
        company_counts_by_stage=(),
        company_counts_by_category=(),
        contact_counts_by_stage=(),
        contact_counts_by_category=(),
        overdue_actions=(),
        upcoming_actions=(),
        recent_evidence=(),
        stale_evidence=(),
        without_decision_maker=(),
        without_next_action=(),
        generated_at=NOW,
    )
    assert dashboard_is_empty(empty) is True
    populated = AcquisitionDashboardData(
        company_counts_by_stage=(CountBucket(key="seed", label="Seed", count=2),),
        company_counts_by_category=(),
        contact_counts_by_stage=(),
        contact_counts_by_category=(),
        overdue_actions=(),
        upcoming_actions=(),
        recent_evidence=(),
        stale_evidence=(),
        without_decision_maker=(),
        without_next_action=(),
        generated_at=NOW,
    )
    assert dashboard_is_empty(populated) is False


@pytest.mark.unit
def test_count_companies_by_stage_sql() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([{"bucket": "seed", "total": 3}])
    rows = repo.count_companies_by_dimension(conn, "stage")
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "GROUP BY bucket" in sql
    assert "archived_at IS NULL" in sql
    assert rows == [("seed", 3)]


@pytest.mark.unit
def test_count_contacts_by_category_sql() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([{"bucket": "fintech", "total": 5}])
    rows = repo.count_contacts_by_company_dimension(conn, "category")
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "FROM contacts ct" in sql
    assert "INNER JOIN companies c" in sql
    assert rows == [("fintech", 5)]


@pytest.mark.unit
def test_overdue_next_actions_sql_is_bounded() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    repo.list_overdue_next_actions(conn, reference=NOW, limit=20)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "record_type = 'follow_up_note'" in sql
    assert "review_at <" in sql
    assert "LIMIT" in sql
    assert conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == (NOW, 20)


@pytest.mark.unit
def test_stale_evidence_sql_uses_public_types() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    repo.list_stale_evidence(conn, reference=NOW, limit=15)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "expires_at <= %s" in sql
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1]
    assert params[0] == ["verified_fact", "public_signal"]
    assert params[1] == NOW
    assert params[2] == 15


@pytest.mark.unit
def test_companies_without_decision_maker_sql() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    repo.list_companies_without_decision_maker(conn, limit=10)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "NOT EXISTS" in sql
    assert "FROM contacts ct" in sql
    assert METRIC_WITHOUT_DECISION_MAKER.startswith("Non-archived")


@pytest.mark.unit
def test_load_acquisition_dashboard_maps_repository_rows() -> None:
    repo = MagicMock()
    repo.count_companies_by_dimension.side_effect = [
        [("seed", 2)],
        [("fintech", 1)],
    ]
    repo.count_contacts_by_company_dimension.side_effect = [
        [("seed", 4)],
        [("fintech", 3)],
    ]
    repo.list_overdue_next_actions.return_value = [
        {
            "id": COMPANY_ID,
            "company_id": COMPANY_ID,
            "company_name": "Acme",
            "contact_name": "Pat",
            "body": "Call back",
            "review_at": NOW,
        }
    ]
    repo.list_upcoming_next_actions.return_value = []
    repo.list_recent_evidence.return_value = []
    repo.list_stale_evidence.return_value = []
    repo.list_companies_without_decision_maker.return_value = []
    repo.list_companies_without_next_action.return_value = []

    data = load_acquisition_dashboard(MagicMock(), repo, now=NOW)
    assert data.company_counts_by_stage[0].label == "Seed"
    assert data.overdue_actions[0].company_name == "Acme"
    assert data.generated_at == NOW


@pytest.mark.unit
def test_load_acquisition_dashboard_parses_string_and_naive_datetimes() -> None:
    repo = MagicMock()
    repo.count_companies_by_dimension.side_effect = [[("unspecified", 1)], []]
    repo.count_contacts_by_company_dimension.side_effect = [[("custom_stage", 2)], []]
    repo.list_overdue_next_actions.return_value = [
        {
            "id": COMPANY_ID,
            "company_id": COMPANY_ID,
            "company_name": "Acme",
            "contact_name": None,
            "body": "Ping",
            "review_at": "2026-07-10T08:00:00Z",
        }
    ]
    repo.list_upcoming_next_actions.return_value = []
    repo.list_recent_evidence.return_value = [
        {
            "id": COMPANY_ID,
            "company_id": COMPANY_ID,
            "company_name": "Beta",
            "record_type": "verified_fact",
            "body": "Fact",
            "created_at": datetime(2026, 7, 1, 9, 0),
            "expires_at": "2026-08-01T00:00:00Z",
        }
    ]
    repo.list_stale_evidence.return_value = []
    repo.list_companies_without_decision_maker.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Gamma",
            "target_status": "target",
            "category": "other",
            "stage": None,
        }
    ]
    repo.list_companies_without_next_action.return_value = []

    data = load_acquisition_dashboard(MagicMock(), repo, now=NOW)
    assert data.company_counts_by_stage[0].label == "Unspecified"
    assert data.contact_counts_by_stage[0].label == "Custom Stage"
    assert data.overdue_actions[0].review_at.tzinfo == timezone.utc
    assert data.recent_evidence[0].created_at.tzinfo == timezone.utc
    assert data.recent_evidence[0].expires_at is not None
    assert data.without_decision_maker[0].company_name == "Gamma"


@pytest.mark.unit
def test_upcoming_next_actions_sql_is_bounded() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    window_end = NOW + timedelta(days=14)
    repo.list_upcoming_next_actions(
        conn,
        reference=NOW,
        window_end=window_end,
        limit=12,
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "review_at >=" in sql
    assert "review_at <=" in sql
    assert conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == (
        NOW,
        window_end,
        12,
    )


@pytest.mark.unit
def test_recent_evidence_sql_orders_by_created_at() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    repo.list_recent_evidence(conn, limit=8)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY rr.created_at DESC" in sql
    assert conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1] == (
        ["verified_fact", "public_signal"],
        8,
    )


@pytest.mark.unit
def test_companies_without_next_action_sql() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    repo.list_companies_without_next_action(conn, limit=5)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "follow_up_note" in sql
    assert "review_at IS NOT NULL" in sql


@pytest.mark.unit
def test_unsupported_dimension_raises() -> None:
    repo = PostgresAcquisitionDashboardRepository()
    conn = _mock_conn([])
    with pytest.raises(ValueError, match="unsupported company dimension"):
        repo.count_companies_by_dimension(conn, "invalid")
    with pytest.raises(ValueError, match="unsupported contact dimension"):
        repo.count_contacts_by_company_dimension(conn, "invalid")
