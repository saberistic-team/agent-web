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


@pytest.mark.unit
def test_inbox_filter_and_model_validators() -> None:
    from pydantic import ValidationError

    from app.discovery_inbox import (
        DiscoveryCandidateAccept,
        DiscoveryCandidateDefer,
        DiscoveryCandidateReject,
        DiscoveryInboxFilters,
        confidence_matches_filter,
        freshness_matches_filter,
    )

    filters = DiscoveryInboxFilters(
        review_state=" Accepted ",
        confidence="HIGH",
        freshness="Fresh",
        source=" yc ",
    )
    assert filters.review_state == "accepted"
    assert filters.confidence == "high"
    assert filters.freshness == "fresh"

    with pytest.raises(ValidationError):
        DiscoveryInboxFilters(review_state="nope")
    with pytest.raises(ValidationError):
        DiscoveryInboxFilters(confidence="extreme")
    with pytest.raises(ValidationError):
        DiscoveryInboxFilters(freshness="ancient")

    assert DiscoveryInboxFilters(review_state="  ").review_state is None
    assert DiscoveryInboxFilters(confidence="").confidence is None
    assert DiscoveryInboxFilters(freshness=None).freshness is None

    accept = DiscoveryCandidateAccept(company_choice="existing", selected_company_id=str(COMPANY_ID))
    assert accept.selected_company_id == str(COMPANY_ID)
    with pytest.raises(ValidationError):
        DiscoveryCandidateAccept(company_choice="existing", selected_company_id=None)

    DiscoveryCandidateReject(rejection_reason="Not a fit")
    future = datetime.now(timezone.utc) + timedelta(days=3)
    DiscoveryCandidateDefer(deferred_until=future)
    with pytest.raises(ValidationError):
        DiscoveryCandidateDefer(deferred_until=datetime.now(timezone.utc) - timedelta(days=1))

    assert confidence_matches_filter(0.9, None) is True
    assert confidence_matches_filter(0.9, "high") is True
    assert confidence_matches_filter(0.6, "medium") is True
    assert confidence_matches_filter(0.2, "low") is True
    assert confidence_matches_filter(None, "low") is True
    assert confidence_matches_filter(None, "high") is False
    assert freshness_matches_filter(datetime.now(timezone.utc) - timedelta(days=1), None) is True
    assert freshness_matches_filter(datetime.now(timezone.utc) - timedelta(days=1), "fresh") is True
    assert freshness_bucket(None) == "stale"
    naive = datetime(2026, 7, 10)
    aware = datetime(2026, 7, 10, tzinfo=timezone.utc)
    reference = datetime(2026, 7, 14, tzinfo=timezone.utc)
    assert freshness_bucket(naive, now=reference) == freshness_bucket(aware, now=reference)


@pytest.mark.unit
def test_get_candidate_detail_builds_suggestions_and_conflicts() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    companies = MagicMock()
    inbox_repo.get_candidate.return_value = {
        "id": CANDIDATE_ID,
        "name": "Nimbus Analytics",
        "domain": "nimbus.example",
        "match_suggestions": [],
        "conflicts": [],
    }
    companies.find_by_domain.return_value = [
        {"id": COMPANY_ID, "name": "Nimbus Analytics", "domain": "other.example"}
    ]
    companies.find_by_exact_name.return_value = [
        {"id": COMPANY_ID, "name": "Nimbus Analytics", "domain": "other.example"}
    ]
    service = DiscoveryInboxService(
        repos=MagicMock(inbox=inbox_repo, crm=MagicMock(companies=companies))
    )
    detail = service.get_candidate_detail(MagicMock(), CANDIDATE_ID)
    assert detail is not None
    assert len(detail["match_suggestions"]) == 1
    assert any("Domain mismatch" in item for item in detail["conflicts"])
    assert service.get_candidate_detail(MagicMock(), CANDIDATE_ID)  # covered path
    inbox_repo.get_candidate.return_value = None
    assert service.get_candidate_detail(MagicMock(), CANDIDATE_ID) is None


@pytest.mark.unit
def test_accept_existing_company_and_error_paths() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    companies = MagicMock()
    source_records = MagicMock()
    research_records = MagicMock()
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture_api:abc",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp1",
        "name": "Nimbus Analytics",
        "domain": "nimbus.example",
        "category": "fintech",
        "confidence": 0.88,
        "review_state": "pending",
        "evidence": {
            "snippet": "Seed round",
            "observations": [
                {
                    "value": "name=Nimbus",
                    "raw_source_id": "fixture_api",
                    "source_url": "https://example.com",
                    "confidence": 0.8,
                },
                "skip-me",
            ],
        },
    }
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = {**candidate, "review_state": "accepted"}
    companies.get_by_id.return_value = {"id": COMPANY_ID, "name": "Nimbus Analytics"}
    source_records.create.return_value = {"id": uuid4()}
    research_records.create.return_value = {"id": uuid4()}

    service = DiscoveryInboxService(
        repos=MagicMock(
            inbox=inbox_repo,
            crm=MagicMock(
                companies=companies,
                source_records=source_records,
                research_records=research_records,
                pipeline=MagicMock(),
            ),
        )
    )
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_accept"), patch.object(
        audit_service, "record_research_record_create"
    ):
        result = service.accept_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            company_choice="existing",
            selected_company_id=COMPANY_ID,
        )
    assert result["company"]["id"] == COMPANY_ID

    from app.discovery_inbox import DiscoveryCandidateNotFoundError, DiscoveryCandidateStateError, DiscoveryInboxError

    with pytest.raises(DiscoveryInboxError):
        service.accept_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            company_choice="existing",
            selected_company_id=None,
        )
    companies.get_by_id.return_value = None
    with pytest.raises(DiscoveryInboxError):
        service.accept_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            company_choice="existing",
            selected_company_id=COMPANY_ID,
        )

    inbox_repo.get_candidate.return_value = None
    with pytest.raises(DiscoveryCandidateNotFoundError):
        service.reject_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            rejection_reason="nope",
        )
    inbox_repo.get_candidate.return_value = {**candidate, "review_state": "accepted"}
    with pytest.raises(DiscoveryCandidateStateError):
        service.reject_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            rejection_reason="nope",
        )


@pytest.mark.unit
def test_bulk_preview_and_commit_paths() -> None:
    from app.discovery_inbox import DiscoveryCandidateNotFoundError, DiscoveryInboxError

    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    pending = {
        "id": CANDIDATE_ID,
        "name": "Nimbus",
        "source_id": "yc",
        "domain": "nimbus.example",
        "review_state": "pending",
        "external_id": "yc:1",
        "evidence_fingerprint": "fp",
        "evidence": None,
        "category": "fintech",
        "website": None,
    }
    inbox_repo.get_candidates_by_ids.return_value = [pending]
    inbox_repo.get_candidate.return_value = pending
    inbox_repo.update_candidate_review.return_value = {**pending, "review_state": "rejected"}
    inbox_repo.record_rejection_suppression.return_value = {"id": uuid4()}

    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock(
        companies=MagicMock(create=MagicMock(return_value={"id": COMPANY_ID})),
        pipeline=MagicMock(),
        source_records=MagicMock(create=MagicMock(return_value={"id": uuid4()})),
        research_records=MagicMock(),
    )))
    conn = MagicMock()
    preview = service.preview_bulk_action(
        conn,
        action="reject",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason="Not ICP",
    )
    assert preview["count"] == 1
    assert preview["preview_token"]

    with patch.object(audit_service, "record_discovery_candidate_reject"), patch.object(
        audit_service, "record_discovery_candidate_bulk"
    ):
        committed = service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="reject",
            candidate_ids=[CANDIDATE_ID],
            preview_token=preview["preview_token"],
            rejection_reason="Not ICP",
        )
    assert committed["count"] == 1

    with pytest.raises(DiscoveryInboxError):
        service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="reject",
            candidate_ids=[CANDIDATE_ID],
            preview_token="bad",
            rejection_reason="Not ICP",
        )
    with pytest.raises(DiscoveryInboxError):
        service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="reject",
            candidate_ids=[CANDIDATE_ID],
            preview_token=DiscoveryInboxService._bulk_preview_token(
                action="reject",
                candidate_ids=[CANDIDATE_ID],
                rejection_reason=None,
                deferred_until=None,
            ),
            rejection_reason=None,
        )
    future = datetime.now(timezone.utc) + timedelta(days=5)
    defer_token = DiscoveryInboxService._bulk_preview_token(
        action="defer",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason=None,
        deferred_until=future,
    )
    with pytest.raises(DiscoveryInboxError):
        service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="defer",
            candidate_ids=[CANDIDATE_ID],
            preview_token=defer_token,
            deferred_until=None,
        )

    inbox_repo.get_candidates_by_ids.return_value = []
    with pytest.raises(DiscoveryCandidateNotFoundError):
        service.preview_bulk_action(conn, action="accept", candidate_ids=[CANDIDATE_ID])

    with pytest.raises(DiscoveryBulkLimitError):
        service.preview_bulk_action(conn, action="accept", candidate_ids=[])

    list_repo = MagicMock()
    list_repo.list_sources.return_value = ["yc"]
    list_repo.list_runs.return_value = []
    list_repo.list_candidates.return_value = [{"id": CANDIDATE_ID}]
    metadata_service = DiscoveryInboxService(
        repos=MagicMock(inbox=list_repo, crm=MagicMock())
    )
    assert metadata_service.list_filter_metadata(conn) == {"sources": ["yc"], "runs": []}
    assert metadata_service.list_candidates(conn) == [{"id": CANDIDATE_ID}]


@pytest.mark.unit
def test_bulk_accept_and_defer_commit() -> None:
    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    companies = MagicMock()
    pipeline = MagicMock()
    source_records = MagicMock()
    research_records = MagicMock()
    pending = {
        "id": CANDIDATE_ID,
        "name": "Nimbus",
        "source_id": "yc",
        "domain": "nimbus.example",
        "website": None,
        "category": "fintech",
        "confidence": 0.7,
        "review_state": "deferred",
        "external_id": "yc:1",
        "evidence_fingerprint": "fp",
        "evidence": None,
    }
    inbox_repo.get_candidate.return_value = pending
    inbox_repo.update_candidate_review.return_value = {**pending, "review_state": "accepted"}
    companies.create.return_value = {"id": COMPANY_ID, "name": "Nimbus"}
    source_records.create.return_value = {"id": uuid4()}

    service = DiscoveryInboxService(
        repos=MagicMock(
            inbox=inbox_repo,
            crm=MagicMock(
                companies=companies,
                pipeline=pipeline,
                source_records=source_records,
                research_records=research_records,
            ),
        )
    )
    conn = MagicMock()
    accept_token = DiscoveryInboxService._bulk_preview_token(
        action="accept",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason=None,
        deferred_until=None,
    )
    with patch.object(audit_service, "record_discovery_candidate_accept"), patch.object(
        audit_service, "record_discovery_candidate_bulk"
    ):
        out = service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="accept",
            candidate_ids=[CANDIDATE_ID],
            preview_token=accept_token,
        )
    assert out["action"] == "accept"

    future = datetime.now(timezone.utc) + timedelta(days=2)
    inbox_repo.get_candidate.return_value = {**pending, "review_state": "pending"}
    inbox_repo.update_candidate_review.return_value = {**pending, "review_state": "deferred"}
    defer_token = DiscoveryInboxService._bulk_preview_token(
        action="defer",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason=None,
        deferred_until=future,
    )
    with patch.object(audit_service, "record_discovery_candidate_defer"), patch.object(
        audit_service, "record_discovery_candidate_bulk"
    ):
        deferred = service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="defer",
            candidate_ids=[CANDIDATE_ID],
            preview_token=defer_token,
            deferred_until=future,
        )
    assert deferred["action"] == "defer"

    from app.repositories.discovery_inbox_postgres import discovery_filter_options

    options = discovery_filter_options()
    assert "freshness" in options and "confidence" in options


@pytest.mark.unit
def test_bulk_defer_requires_deferred_until_with_matching_token() -> None:
    from app.discovery_inbox import DiscoveryInboxError

    inbox_repo = MagicMock(spec=PostgresDiscoveryInboxRepository)
    inbox_repo.get_candidate.return_value = {
        "id": CANDIDATE_ID,
        "review_state": "pending",
        "source_id": "yc",
        "external_id": "yc:1",
        "evidence_fingerprint": "fp",
        "name": "Nimbus",
    }
    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock()))
    token = DiscoveryInboxService._bulk_preview_token(
        action="defer",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason=None,
        deferred_until=None,
    )
    with pytest.raises(DiscoveryInboxError, match="deferred_until is required"):
        service.commit_bulk_action(
            MagicMock(),
            actor_context=ACTOR,
            action="defer",
            candidate_ids=[CANDIDATE_ID],
            preview_token=token,
            deferred_until=None,
        )
