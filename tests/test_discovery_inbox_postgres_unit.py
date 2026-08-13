"""Unit tests for discovery inbox Postgres repository (#122)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.discovery_inbox import DiscoveryInboxFilters
from app.repositories.discovery_inbox_postgres import (
    PostgresDiscoveryInboxRepository,
    _confidence_sql,
    _freshness_sql,
)

CANDIDATE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RUN_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _mock_conn(row: dict | list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(row, list):
        cur.fetchall.return_value = row
        cur.fetchone.return_value = row[0] if row else None
    elif row is not None:
        cur.fetchone.return_value = row
        cur.fetchall.return_value = [row]
    else:
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
    return conn


@pytest.mark.unit
def test_confidence_and_freshness_sql_helpers() -> None:
    assert _confidence_sql(None) == ("", [])
    assert _confidence_sql("high")[0].startswith(" AND confidence >=")
    assert _confidence_sql("medium")[0].startswith(" AND confidence >=")
    assert "confidence IS NULL" in _confidence_sql("low")[0]

    assert _freshness_sql(None) == ("", [])
    assert "discovered_at >=" in _freshness_sql("fresh")[0]
    assert "discovered_at >=" in _freshness_sql("recent")[0]
    assert "discovered_at >=" in _freshness_sql("aging")[0]
    assert "discovered_at <" in _freshness_sql("stale")[0]


@pytest.mark.unit
def test_list_runs_maps_shipped_discovery_runs_schema() -> None:
    repo = PostgresDiscoveryInboxRepository()
    row = {
        "id": RUN_ID,
        "source_id": "yc",
        "started_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        "status": "completed",
        "candidate_count": 2,
    }
    conn = _mock_conn([row])
    runs = repo.list_runs(conn, limit=10)
    assert runs == [row]
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "FROM discovery_runs r" in sql
    assert "finished_at AS completed_at" in sql
    assert "discovery_run_sources" in sql


@pytest.mark.unit
def test_list_sources_and_candidates_with_filters() -> None:
    repo = PostgresDiscoveryInboxRepository()
    conn = _mock_conn([{"source_id": "yc"}, {"source_id": "fixture_api"}])
    assert repo.list_sources(conn) == ["yc", "fixture_api"]

    now = datetime.now(timezone.utc)
    candidate = {
        "id": CANDIDATE_ID,
        "source_id": "yc",
        "discovered_at": now - timedelta(days=2),
        "review_state": "pending",
        "confidence": 0.9,
    }
    conn2 = _mock_conn([candidate])
    rows = repo.list_candidates(
        conn2,
        filters=DiscoveryInboxFilters(
            source="yc",
            run_id=str(RUN_ID),
            category="fintech",
            confidence="high",
            freshness="fresh",
            review_state="pending",
        ),
        limit=50,
    )
    assert len(rows) == 1
    assert rows[0]["freshness"] == "fresh"
    sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "discovery_candidates" in sql
    assert "discovery_rejection_suppressions" in sql


@pytest.mark.unit
def test_list_candidates_deferred_and_empty_filters() -> None:
    repo = PostgresDiscoveryInboxRepository()
    conn = _mock_conn([])
    repo.list_candidates(conn, filters=DiscoveryInboxFilters(review_state="deferred"))
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "review_state = 'deferred'" in sql

    conn2 = _mock_conn([])
    repo.list_candidates(conn2, filters=DiscoveryInboxFilters(confidence="medium", freshness="stale"))
    sql2 = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "confidence >=" in sql2
    assert "discovered_at <" in sql2


@pytest.mark.unit
def test_get_candidate_and_by_ids() -> None:
    repo = PostgresDiscoveryInboxRepository()
    row = {
        "id": CANDIDATE_ID,
        "discovered_at": datetime.now(timezone.utc),
        "name": "Nimbus",
    }
    conn = _mock_conn(row)
    assert repo.get_candidate(conn, CANDIDATE_ID)["name"] == "Nimbus"
    assert repo.get_candidate(_mock_conn(None), CANDIDATE_ID) is None

    assert repo.get_candidates_by_ids(_mock_conn(), []) == []
    conn2 = _mock_conn([row])
    rows = repo.get_candidates_by_ids(conn2, [CANDIDATE_ID])
    assert rows[0]["id"] == CANDIDATE_ID
    sql = str(conn2.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "ANY(%s)" in sql


@pytest.mark.unit
def test_suppression_update_insert_helpers() -> None:
    repo = PostgresDiscoveryInboxRepository()
    conn = _mock_conn({"ok": 1})
    assert repo.is_suppressed(
        conn,
        source_id="yc",
        external_id="yc:1",
        evidence_fingerprint="fp",
    )
    assert not repo.is_suppressed(
        _mock_conn(None),
        source_id="yc",
        external_id="yc:1",
        evidence_fingerprint="fp",
    )

    updated = {"id": CANDIDATE_ID, "review_state": "accepted"}
    conn2 = _mock_conn(updated)
    out = repo.update_candidate_review(
        conn2,
        CANDIDATE_ID,
        review_state="accepted",
        reviewed_by="operator",
        linked_company_id=CANDIDATE_ID,
    )
    assert out is not None
    assert out["review_state"] == "accepted"

    assert (
        repo.update_candidate_review(
            _mock_conn(None),
            CANDIDATE_ID,
            review_state="rejected",
            reviewed_by="operator",
            rejection_reason="nope",
        )
        is None
    )

    inserted = {"id": CANDIDATE_ID, "name": "Nimbus"}
    conn3 = _mock_conn(inserted)
    created = repo.insert_candidate(
        conn3,
        run_id=RUN_ID,
        source_id="yc",
        external_id="yc:1",
        evidence_fingerprint="fp",
        name="Nimbus",
        domain="nimbus.example",
        website="https://nimbus.example",
        category="fintech",
        confidence=0.9,
        signals=["hiring"],
        evidence={"snippet": "x"},
        raw_payload={"raw": True},
        conflicts={"items": []},
        match_suggestions={"items": []},
    )
    assert created["name"] == "Nimbus"
    sql = str(conn3.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "INSERT INTO discovery_candidates" in sql

    suppression = {"id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")}
    conn4 = _mock_conn(suppression)
    recorded = repo.record_rejection_suppression(
        conn4,
        source_id="yc",
        external_id="yc:1",
        evidence_fingerprint="fp",
        rejection_reason="Not ICP",
        rejected_by="operator",
        candidate_id=CANDIDATE_ID,
    )
    assert recorded["id"] == suppression["id"]
    suppression_sql = str(
        conn4.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    )
    assert "discovery_rejection_suppressions" in suppression_sql


@pytest.mark.unit
def test_upsert_candidate_conflicts_without_touching_review_state() -> None:
    repo = PostgresDiscoveryInboxRepository()
    row = {"id": CANDIDATE_ID, "name": "Nimbus", "inserted": False}
    conn = _mock_conn(row)
    result = repo.upsert_candidate(
        conn,
        run_id=RUN_ID,
        source_id="yc",
        external_id="yc:1",
        evidence_fingerprint="fp",
        name="Nimbus",
        domain="nimbus.example",
        website="https://nimbus.example",
        category="fintech",
        confidence=0.9,
        signals=["hiring"],
        evidence={"snippet": "x"},
        raw_payload={"raw": True},
    )
    assert result == row
    cur = conn.cursor.return_value.__enter__.return_value
    sql = str(cur.execute.call_args.args[0])
    assert "INSERT INTO discovery_candidates" in sql
    assert "ON CONFLICT (source_id, external_id, evidence_fingerprint) DO UPDATE" in sql
    assert "RETURNING *, (xmax = 0) AS inserted" in sql
    assert "review_state" not in sql
    assert "reviewed_by" not in sql
    params = cur.execute.call_args.args[1]
    assert params[0] == RUN_ID
    assert params[1:4] == ("yc", "yc:1", "fp")
