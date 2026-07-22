"""Unit tests for discovery reconciliation Postgres repositories (#121)."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.repositories.discovery_reconcile_postgres import (
    PostgresDiscoveryMergeDecisionRepository,
    PostgresDiscoveryReviewRepository,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _mock_conn(row: dict | list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(row, list):
        cur.fetchall.return_value = row
    elif row is not None:
        cur.fetchone.return_value = row
    else:
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
    return conn


@pytest.mark.unit
def test_discovery_review_count_pending() -> None:
    repo = PostgresDiscoveryReviewRepository()
    conn = _mock_conn({"count": 3})
    assert repo.count_pending(conn) == 3
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "discovery_review_queue" in sql
    assert "status = 'pending'" in sql

    empty = _mock_conn(None)
    assert repo.count_pending(empty) == 0


@pytest.mark.unit
def test_discovery_review_list_pending() -> None:
    repo = PostgresDiscoveryReviewRepository()
    row = {
        "external_id": "yc:alpha",
        "candidate_name": "Alpha",
        "status": "pending",
    }
    conn = _mock_conn([row])
    rows = repo.list_pending(conn, limit=10)
    assert rows == [row]
    cur = conn.cursor.return_value.__enter__.return_value
    sql = str(cur.execute.call_args.args[0])
    assert "ORDER BY created_at ASC" in sql
    assert cur.execute.call_args.args[1] == (10,)


@pytest.mark.unit
def test_discovery_review_upsert_and_resolve() -> None:
    repo = PostgresDiscoveryReviewRepository()
    upserted = {
        "external_id": "yc:beta",
        "candidate_name": "Beta",
        "status": "pending",
    }
    conn = _mock_conn(upserted)
    result = repo.upsert_pending(
        conn,
        external_id="yc:beta",
        source_id="yc",
        candidate_name="Beta",
        candidate_domain="beta.example.com",
        candidate_payload={"name": "Beta"},
        reason="name_match",
        match_tier="name",
        candidate_company_ids=[str(COMPANY_ID)],
    )
    assert result["external_id"] == "yc:beta"
    cur = conn.cursor.return_value.__enter__.return_value
    sql = str(cur.execute.call_args.args[0])
    assert "INSERT INTO discovery_review_queue" in sql
    assert "ON CONFLICT (external_id) WHERE status = 'pending'" in sql

    resolved = {**upserted, "status": "resolved", "resolved_by": "operator"}
    conn2 = _mock_conn(resolved)
    out = repo.resolve(
        conn2,
        external_id="yc:beta",
        company_id=COMPANY_ID,
        resolved_by="operator",
    )
    assert out is not None
    assert out["status"] == "resolved"
    resolve_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE discovery_review_queue" in resolve_sql

    conn3 = _mock_conn(None)
    assert (
        repo.resolve(
            conn3,
            external_id="missing",
            company_id=None,
            resolved_by="operator",
        )
        is None
    )


@pytest.mark.unit
def test_discovery_merge_decision_create_and_get_latest() -> None:
    repo = PostgresDiscoveryMergeDecisionRepository()
    created = {
        "external_id": "yc:gamma",
        "decision": "link",
        "company_id": COMPANY_ID,
        "actor": "operator",
    }
    conn = _mock_conn(created)
    row = repo.create(
        conn,
        external_id="yc:gamma",
        source_id="yc",
        decision="link",
        company_id=COMPANY_ID,
        candidate_domain="gamma.example.com",
        candidate_name="Gamma",
        match_tier="override",
        actor="operator",
        correlation_id="corr-1",
        notes="manual link",
    )
    assert row["decision"] == "link"
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO discovery_merge_decisions" in sql

    conn2 = _mock_conn(created)
    latest = repo.get_latest(conn2, external_id="yc:gamma")
    assert latest is not None
    assert latest["external_id"] == "yc:gamma"
    latest_sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ORDER BY created_at DESC" in latest_sql

    conn3 = _mock_conn(None)
    assert repo.get_latest(conn3, external_id="missing") is None
