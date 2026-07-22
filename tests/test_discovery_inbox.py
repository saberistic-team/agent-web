"""Unit tests for lead discovery inbox domain and service logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app import audit_service
from app.actor_context import ActorContext
from app.discovery_inbox import (
    DISCOVERY_BULK_MAX,
    DiscoveryBulkLimitError,
    compute_evidence_fingerprint,
    freshness_bucket,
)
from app.discovery_inbox_service import DiscoveryInboxService
from app.repositories.discovery_inbox_postgres import PostgresDiscoveryInboxRepository

CANDIDATE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ACTOR = ActorContext(actor="operator", correlation_id="corr-discovery")


@pytest.mark.unit
def test_compute_evidence_fingerprint_changes_with_evidence() -> None:
    base = compute_evidence_fingerprint(None, external_id="fixture:1")
    changed = compute_evidence_fingerprint(
        {"observations": [{"value": "name=Acme"}]},
        external_id="fixture:1",
    )
    assert base != changed


@pytest.mark.unit
def test_freshness_bucket_boundaries() -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    assert freshness_bucket(now - timedelta(days=3), now=now) == "fresh"
    assert freshness_bucket(now - timedelta(days=20), now=now) == "recent"
    assert freshness_bucket(now - timedelta(days=60), now=now) == "aging"
    assert freshness_bucket(now - timedelta(days=120), now=now) == "stale"


@pytest.mark.unit
def test_accept_candidate_creates_company_and_source_record() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    companies = MagicMock()
    pipeline = MagicMock()
    source_records = MagicMock()
    research_records = MagicMock()
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture_api:abc",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp1",
        "name": "Nimbus Analytics",
        "domain": "nimbus.example",
        "website": "https://nimbus.example",
        "category": "fintech",
        "confidence": 0.88,
        "review_state": "pending",
        "evidence": {"snippet": "Seed round announced", "observations": []},
    }
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = {**candidate, "review_state": "accepted"}
    companies.create.return_value = {"id": COMPANY_ID, "name": "Nimbus Analytics"}
    source_records.create.return_value = {"id": uuid4()}

    service = DiscoveryInboxService(
        repos=MagicMock(inbox=inbox_repo, crm=MagicMock(
            companies=companies,
            pipeline=pipeline,
            source_records=source_records,
            research_records=research_records,
        ))
    )
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_accept") as audit_accept:
        result = service.accept_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            company_choice="new",
        )
    assert result["company"]["id"] == COMPANY_ID
    companies.create.assert_called_once()
    pipeline.update_pipeline_fields.assert_called_once()
    source_records.create.assert_called_once()
    audit_accept.assert_called_once()


@pytest.mark.unit
def test_reject_candidate_records_suppression() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture_api:abc",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp1",
        "review_state": "pending",
    }
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = {**candidate, "review_state": "rejected"}
    inbox_repo.record_rejection_suppression.return_value = {"id": uuid4()}

    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock()))
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_reject") as audit_reject:
        service.reject_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            rejection_reason="Not ICP fit",
        )
    inbox_repo.record_rejection_suppression.assert_called_once()
    audit_reject.assert_called_once()


@pytest.mark.unit
def test_defer_candidate_sets_review_date() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    candidate = {"id": CANDIDATE_ID, "review_state": "pending"}
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = {**candidate, "review_state": "deferred"}
    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock()))
    deferred_until = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_defer") as audit_defer:
        service.defer_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            deferred_until=deferred_until,
        )
    audit_defer.assert_called_once()


@pytest.mark.unit
def test_bulk_preview_token_is_deterministic() -> None:
    ids = [CANDIDATE_ID]
    first = DiscoveryInboxService._bulk_preview_token(
        action="reject",
        candidate_ids=ids,
        rejection_reason="duplicate",
        deferred_until=None,
    )
    second = DiscoveryInboxService._bulk_preview_token(
        action="reject",
        candidate_ids=ids,
        rejection_reason="duplicate",
        deferred_until=None,
    )
    assert first == second


@pytest.mark.unit
def test_bulk_selection_limit() -> None:
    service = DiscoveryInboxService()
    too_many = [UUID(int=i) for i in range(DISCOVERY_BULK_MAX + 1)]
    with pytest.raises(DiscoveryBulkLimitError):
        service.preview_bulk_action(MagicMock(), action="reject", candidate_ids=too_many)
