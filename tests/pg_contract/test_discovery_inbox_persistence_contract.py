"""Discovery run → review inbox persistence against real PostgreSQL.

Proves the run-to-inbox wiring end to end: run candidates land in
``discovery_candidates``, identical evidence re-runs refresh the same row
without resetting operator review state, rejection suppression hides identical
candidates, and materially changed evidence re-surfaces as a new pending row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.orchestrator import DiscoveryRunConfig, execute_discovery_run
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCandidate,
    DiscoveryCheckpoint,
    DiscoveryEvidence,
    DiscoveryObservation,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)
from app.discovery_inbox import DiscoveryInboxFilters
from app.repositories.discovery_inbox_postgres import PostgresDiscoveryInboxRepository

pytestmark = pytest.mark.contract

_SOURCE_ID = "contract_stub"


@dataclass(frozen=True)
class _StubAdapter:
    identity: SourceIdentity
    terms: TermsReviewMetadata
    access: AccessDocumentation
    handler: Callable[[DiscoveryCheckpoint | None], DiscoveryRunResult]

    @property
    def is_operational(self) -> bool:
        return True

    def discover(self, *, checkpoint=None, fetcher=None):  # type: ignore[no-untyped-def]
        return self.handler(checkpoint)


def _observation(value: str, *, retrieved_at: str, confidence: float) -> DiscoveryObservation:
    return DiscoveryObservation(
        source_url="https://directory.example.com/companies",
        retrieved_at=retrieved_at,
        raw_source_id=_SOURCE_ID,
        value=value,
        confidence=confidence,
        review_at=None,
        expires_at=None,
    )


def _candidate(
    external_id: str,
    name: str,
    *,
    website: str,
    retrieved_at: str,
) -> DiscoveryCandidate:
    observations = (
        _observation(f"name={name}", retrieved_at=retrieved_at, confidence=0.95),
        _observation(f"website={website}", retrieved_at=retrieved_at, confidence=0.9),
    )
    return DiscoveryCandidate(
        external_id=external_id,
        name=name,
        domain=website.removeprefix("https://"),
        website=website,
        signals=(f"source:{_SOURCE_ID}", "category:fintech"),
        evidence=DiscoveryEvidence(
            observations=observations,
            snippet=f"{name} snapshot.",
        ),
        raw_payload={"suggested_category": "fintech"},
    )


def _run_once(
    conn: psycopg.Connection,
    *,
    candidates: list[DiscoveryCandidate],
    cursor: str,
):
    def handler(_checkpoint: DiscoveryCheckpoint | None) -> DiscoveryRunResult:
        return DiscoveryRunResult(
            source_id=_SOURCE_ID,
            candidates=candidates,
            checkpoint=DiscoveryCheckpoint(cursor=cursor),
        )

    adapter = _StubAdapter(
        identity=SourceIdentity(source_id=_SOURCE_ID, display_name=_SOURCE_ID, source_kind="test"),
        terms=TermsReviewMetadata(
            terms_url="https://example.com/terms",
            robots_reviewed_at="2026-01-01T00:00:00+00:00",
            robots_allowed=True,
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.API,
            user_agent="contract-test",
            documented_at="2026-01-01T00:00:00+00:00",
            rate_limit_requests_per_minute=60,
            max_response_bytes=1024,
            timeout_seconds=5.0,
        ),
        handler=handler,
    )
    registry = DiscoverySourceRegistry()
    registry.register(adapter)
    registry.enable(_SOURCE_ID)
    return execute_discovery_run(
        conn,
        registry,
        trigger_type="manual",
        actor="contract-test",
        correlation_id=f"contract-{uuid4()}",
        config=DiscoveryRunConfig(
            retry_max_attempts=1,
            retry_base_seconds=0.0,
            retry_cap_seconds=0.0,
        ),
        enabled_sources=[_SOURCE_ID],
        sleep=lambda _delay: None,
    )


def _candidates_table(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM discovery_candidates WHERE source_id = %s ORDER BY external_id, created_at",
            (_SOURCE_ID,),
        )
        return [dict(row) for row in cur.fetchall()]


def test_upsert_candidate_inserted_flag(migrated_conn: psycopg.Connection) -> None:
    repo = PostgresDiscoveryInboxRepository()
    first = repo.upsert_candidate(
        migrated_conn,
        run_id=None,
        source_id=_SOURCE_ID,
        external_id="contract_stub:flag",
        evidence_fingerprint="fp-flag",
        name="Flag Co",
    )
    assert first["inserted"] is True
    again = repo.upsert_candidate(
        migrated_conn,
        run_id=None,
        source_id=_SOURCE_ID,
        external_id="contract_stub:flag",
        evidence_fingerprint="fp-flag",
        name="Flag Co",
    )
    assert again["inserted"] is False
    assert again["id"] == first["id"]


def test_discovery_run_populates_and_refreshes_inbox(
    migrated_conn: psycopg.Connection,
) -> None:
    first_batch = [
        _candidate(
            "contract_stub:1",
            "Nimbus Analytics",
            website="https://nimbus.example",
            retrieved_at="2026-08-02T06:00:00+00:00",
        ),
        _candidate(
            "contract_stub:2",
            "Ledgerflow",
            website="https://ledgerflow.example",
            retrieved_at="2026-08-02T06:00:00+00:00",
        ),
    ]
    run_one = _run_once(migrated_conn, candidates=first_batch, cursor="1")
    assert run_one.status == "completed"

    rows = _candidates_table(migrated_conn)
    assert len(rows) == 2
    by_external = {row["external_id"]: row for row in rows}
    nimbus = by_external["contract_stub:1"]
    assert nimbus["review_state"] == "pending"
    assert str(nimbus["run_id"]) == str(run_one.run_id)
    assert nimbus["category"] == "fintech"
    assert float(nimbus["confidence"]) == 0.95
    assert len(nimbus["evidence_fingerprint"]) == 32
    assert "category:fintech" in list(nimbus["signals"])
    evidence = nimbus["evidence"]
    assert evidence["snippet"] == "Nimbus Analytics snapshot."
    assert {obs["value"] for obs in evidence["observations"]} == {
        "name=Nimbus Analytics",
        "website=https://nimbus.example",
    }

    # Same evidence, fresh retrieval timestamps → refresh, no duplicates.
    second_batch = [
        _candidate(
            "contract_stub:1",
            "Nimbus Analytics",
            website="https://nimbus.example",
            retrieved_at="2026-08-09T06:00:00+00:00",
        ),
        _candidate(
            "contract_stub:2",
            "Ledgerflow",
            website="https://ledgerflow.example",
            retrieved_at="2026-08-09T06:00:00+00:00",
        ),
    ]
    run_two = _run_once(migrated_conn, candidates=second_batch, cursor="2")
    assert run_two.status == "completed"

    rows = _candidates_table(migrated_conn)
    assert len(rows) == 2
    refreshed = {row["external_id"]: row for row in rows}["contract_stub:1"]
    assert refreshed["id"] == nimbus["id"]
    assert str(refreshed["run_id"]) == str(run_two.run_id)
    assert refreshed["review_state"] == "pending"
    assert refreshed["discovered_at"] >= nimbus["discovered_at"]

    # Operator rejects one candidate; identical re-sighting must not reset it.
    inbox = PostgresDiscoveryInboxRepository()
    ledgerflow = {row["external_id"]: row for row in rows}["contract_stub:2"]
    reviewed = inbox.update_candidate_review(
        migrated_conn,
        ledgerflow["id"],
        review_state="rejected",
        reviewed_by="operator",
        rejection_reason="Not ICP",
    )
    assert reviewed is not None
    inbox.record_rejection_suppression(
        migrated_conn,
        source_id=_SOURCE_ID,
        external_id="contract_stub:2",
        evidence_fingerprint=ledgerflow["evidence_fingerprint"],
        rejection_reason="Not ICP",
        rejected_by="operator",
        candidate_id=ledgerflow["id"],
    )
    migrated_conn.commit()

    run_three = _run_once(migrated_conn, candidates=second_batch, cursor="3")
    assert run_three.status == "completed"
    rows = _candidates_table(migrated_conn)
    assert len(rows) == 2
    still_rejected = {row["external_id"]: row for row in rows}["contract_stub:2"]
    assert still_rejected["review_state"] == "rejected"
    assert still_rejected["rejection_reason"] == "Not ICP"

    # Suppressed + rejected candidate hidden from the unfiltered inbox list.
    visible = inbox.list_candidates(
        migrated_conn,
        filters=DiscoveryInboxFilters(review_state=None),
    )
    assert {row["external_id"] for row in visible} == {"contract_stub:1"}

    # Materially changed evidence re-surfaces as a new pending row.
    changed_batch = [
        _candidate(
            "contract_stub:1",
            "Nimbus Analytics",
            website="https://nimbus.example",
            retrieved_at="2026-08-16T06:00:00+00:00",
        ),
        _candidate(
            "contract_stub:2",
            "Ledgerflow",
            website="https://ledgerflow.io",
            retrieved_at="2026-08-16T06:00:00+00:00",
        ),
    ]
    run_four = _run_once(migrated_conn, candidates=changed_batch, cursor="4")
    assert run_four.status == "completed"

    rows = _candidates_table(migrated_conn)
    assert len(rows) == 3
    ledgerflow_rows = [row for row in rows if row["external_id"] == "contract_stub:2"]
    assert len(ledgerflow_rows) == 2
    states = sorted(row["review_state"] for row in ledgerflow_rows)
    assert states == ["pending", "rejected"]
    resurfaced = next(row for row in ledgerflow_rows if row["review_state"] == "pending")
    assert resurfaced["evidence_fingerprint"] != ledgerflow["evidence_fingerprint"]
    assert str(resurfaced["run_id"]) == str(run_four.run_id)
