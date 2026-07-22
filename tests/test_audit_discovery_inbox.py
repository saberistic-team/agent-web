"""Audit coverage for discovery inbox review actions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app import audit_service
from app.actor_context import ActorContext
from app.discovery_inbox_service import DiscoveryInboxService

CANDIDATE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ACTOR = ActorContext(actor="operator", correlation_id="corr-discovery-audit")


@pytest.mark.unit
def test_discovery_accept_emits_audit_event() -> None:
    inbox_repo = MagicMock()
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture:1",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp",
        "name": "Acme",
        "domain": "acme.example",
        "website": "https://acme.example",
        "category": "fintech",
        "confidence": 0.8,
        "review_state": "pending",
        "evidence": None,
    }
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = candidate
    companies = MagicMock()
    companies.create.return_value = {"id": COMPANY_ID, "name": "Acme"}
    service = DiscoveryInboxService(
        repos=MagicMock(
            inbox=inbox_repo,
            crm=MagicMock(
                companies=companies,
                pipeline=MagicMock(),
                source_records=MagicMock(create=MagicMock(return_value={"id": uuid4()})),
                research_records=MagicMock(),
            ),
        )
    )
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_accept") as audit_accept:
        service.accept_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            company_choice="new",
        )
    audit_accept.assert_called_once()
    assert audit_accept.call_args.kwargs["candidate_id"] == str(CANDIDATE_ID)


@pytest.mark.unit
def test_discovery_reject_emits_audit_event() -> None:
    inbox_repo = MagicMock()
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture:1",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp",
        "review_state": "pending",
    }
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = candidate
    inbox_repo.record_rejection_suppression.return_value = {"id": uuid4()}
    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock()))
    conn = MagicMock()
    with patch.object(audit_service, "record_discovery_candidate_reject") as audit_reject:
        service.reject_candidate(
            conn,
            candidate_id=CANDIDATE_ID,
            actor_context=ACTOR,
            rejection_reason="Duplicate of existing target",
        )
    audit_reject.assert_called_once()


@pytest.mark.unit
def test_discovery_bulk_commit_emits_bulk_audit_event() -> None:
    inbox_repo = MagicMock()
    candidate = {
        "id": CANDIDATE_ID,
        "external_id": "fixture:1",
        "source_id": "fixture_api",
        "evidence_fingerprint": "fp",
        "name": "Acme",
        "domain": "acme.example",
        "website": "https://acme.example",
        "category": "fintech",
        "confidence": 0.8,
        "review_state": "pending",
        "evidence": None,
    }
    inbox_repo.get_candidates_by_ids.return_value = [candidate]
    inbox_repo.get_candidate.return_value = candidate
    inbox_repo.update_candidate_review.return_value = candidate
    inbox_repo.record_rejection_suppression.return_value = {"id": uuid4()}
    service = DiscoveryInboxService(repos=MagicMock(inbox=inbox_repo, crm=MagicMock()))
    preview = service.preview_bulk_action(
        MagicMock(),
        action="reject",
        candidate_ids=[CANDIDATE_ID],
        rejection_reason="Not ICP",
    )
    conn = MagicMock()
    with (
        patch.object(service, "reject_candidate", return_value={"candidate": candidate}) as reject,
        patch.object(audit_service, "record_discovery_candidate_bulk") as audit_bulk,
    ):
        service.commit_bulk_action(
            conn,
            actor_context=ACTOR,
            action="reject",
            candidate_ids=[CANDIDATE_ID],
            preview_token=preview["preview_token"],
            rejection_reason="Not ICP",
        )
    reject.assert_called_once()
    audit_bulk.assert_called_once()
