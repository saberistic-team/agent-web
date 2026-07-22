"""Tests for CrmService discovery reconciliation."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService
from app.discovery.normalize import normalize_candidate
from app.discovery.observation import build_observation

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ACTOR = ActorContext(actor="operator", correlation_id="corr-discovery-1")


def _candidate(**overrides: object):
    observations = overrides.pop("observations", None)
    obs = None
    if observations:
        obs = [
            build_observation(
                source_url="https://example.com/source",
                raw_source_id=item,
                value=f"signal={item}",
                confidence=0.8,
                retrieved_at="2026-01-10T00:00:00+00:00",
            )
            for item in observations
        ]
    return normalize_candidate(
        source_id=str(overrides.pop("source_id", "yc")),
        name=str(overrides.pop("name", "Nimbus Analytics")),
        domain=overrides.pop("domain", "nimbus.example.com"),
        website=overrides.pop("website", "https://nimbus.example.com"),
        signals=list(overrides.pop("signals", ("ai infrastructure",))),
        external_id=str(overrides.pop("external_id", "yc:nimbus")),
        observations=obs,
    )


def _service(
    *,
    companies: MagicMock | None = None,
    source_records: MagicMock | None = None,
    research_records: MagicMock | None = None,
    discovery_review: MagicMock | None = None,
    discovery_merge_decisions: MagicMock | None = None,
) -> tuple[CrmService, MagicMock]:
    repos = {
        "companies": companies or MagicMock(),
        "contacts": MagicMock(),
        "source_records": source_records or MagicMock(),
        "activities": MagicMock(),
        "research_records": research_records or MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": MagicMock(),
        "icp_scoring": MagicMock(),
        "discovery_review": discovery_review or MagicMock(),
        "discovery_merge_decisions": discovery_merge_decisions or MagicMock(),
    }
    return CrmService(repos=CrmRepositories(**repos)), MagicMock()


@pytest.mark.unit
def test_preview_discovery_reconcile_matches_domain_and_preserves_absent() -> None:
    companies = MagicMock()
    companies.list_all.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Nimbus Analytics",
            "domain": "nimbus.example.com",
            "website": "https://nimbus.example.com",
            "field_sources": {},
        },
        {
            "id": OTHER_COMPANY_ID,
            "name": "Legacy Co",
            "domain": "legacy.example.com",
            "website": "https://legacy.example.com",
            "field_sources": {},
        },
    ]
    source_records = MagicMock()
    source_records.get_by_source.return_value = None
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = None
    research_records = MagicMock()
    research_records.list_for_company.return_value = []

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        research_records=research_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )
    preview = service.preview_discovery_reconcile(conn, candidates=[_candidate()])

    assert preview["summary_counts"]["matched"] == 1
    assert preview["absent_preserved"] == 1


@pytest.mark.unit
def test_preview_routes_name_only_candidate_to_review() -> None:
    companies = MagicMock()
    companies.list_all.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Nimbus Analytics",
            "domain": None,
            "website": None,
            "field_sources": {},
        }
    ]
    source_records = MagicMock()
    source_records.get_by_source.return_value = None
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = None

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )
    preview = service.preview_discovery_reconcile(
        conn,
        candidates=[_candidate(domain=None, website=None)],
    )
    assert preview["summary_counts"]["review"] == 1


@pytest.mark.unit
def test_commit_creates_company_and_source_record_without_archiving_absent() -> None:
    companies = MagicMock()
    companies.list_all.return_value = []
    companies.create.return_value = {
        "id": COMPANY_ID,
        "name": "Nimbus Analytics",
        "domain": "nimbus.example.com",
        "field_sources": {},
    }
    source_records = MagicMock()
    source_records.get_by_source.return_value = None
    source_records.create.return_value = {"id": "source-1", "company_id": str(COMPANY_ID)}
    research_records = MagicMock()
    research_records.create.return_value = {"id": "rr-1"}
    research_records.list_for_company.return_value = []
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = None

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        research_records=research_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("app.discovery_reconcile_ops.record_company_create", MagicMock())
        patcher.setattr("app.discovery_reconcile_ops.audit_service.record_research_record_create", MagicMock())
        result = service.commit_discovery_reconcile(
            conn,
            actor_context=ACTOR,
            candidates=[_candidate(observations=["obs-1"])],
            run_id="run-1",
        )

    assert result["summary_counts"]["created"] == 1
    companies.archive.assert_not_called()
    source_records.create.assert_called_once()


@pytest.mark.unit
def test_commit_queues_review_without_creating_company() -> None:
    companies = MagicMock()
    companies.list_all.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Nimbus Analytics",
            "domain": None,
            "website": None,
            "field_sources": {},
        }
    ]
    source_records = MagicMock()
    source_records.get_by_source.return_value = None
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_review.upsert_pending.return_value = {"id": "review-1"}
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = None

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )

    result = service.commit_discovery_reconcile(
        conn,
        actor_context=ACTOR,
        candidates=[_candidate(domain=None, website=None)],
        run_id="run-1",
    )

    assert result["summary_counts"]["review_queued"] == 1
    companies.create.assert_not_called()
    discovery_review.upsert_pending.assert_called_once()


@pytest.mark.unit
def test_repeated_run_refreshes_existing_evidence() -> None:
    companies = MagicMock()
    companies.list_all.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Nimbus Analytics",
            "domain": "nimbus.example.com",
            "website": "https://nimbus.example.com",
            "field_sources": {"category": {"source": "discovery"}},
        }
    ]
    companies.get_by_id.return_value = companies.list_all.return_value[0]
    companies.update.return_value = companies.list_all.return_value[0]
    source_records = MagicMock()
    source_records.get_by_source.return_value = {
        "id": "11111111-1111-1111-1111-111111111111",
        "company_id": str(COMPANY_ID),
    }
    source_records.update_payload.return_value = source_records.get_by_source.return_value
    research_records = MagicMock()
    research_records.list_for_company.return_value = [
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "metadata": {"discovery_observation_key": "https://example.com/source|obs-1"},
        }
    ]
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = None

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        research_records=research_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("app.discovery_reconcile_ops.record_company_update_if_changed", MagicMock())
        first = service.commit_discovery_reconcile(
            conn,
            actor_context=ACTOR,
            candidates=[_candidate(observations=["obs-1"])],
            run_id="run-1",
        )
        second = service.commit_discovery_reconcile(
            conn,
            actor_context=ACTOR,
            candidates=[_candidate(observations=["obs-1"])],
            run_id="run-2",
        )

    assert first["summary_counts"]["matched"] == 1
    assert second["summary_counts"]["matched"] == 1
    research_records.create.assert_not_called()
    assert research_records.update_freshness.call_count == 2


@pytest.mark.unit
def test_user_override_link_decision_is_reused() -> None:
    companies = MagicMock()
    companies.list_all.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Nimbus Analytics",
            "domain": None,
            "website": None,
            "field_sources": {},
        }
    ]
    companies.get_by_id.return_value = companies.list_all.return_value[0]
    companies.update.return_value = companies.list_all.return_value[0]
    source_records = MagicMock()
    source_records.get_by_source.return_value = None
    source_records.create.return_value = {"id": "source-1", "company_id": str(COMPANY_ID)}
    research_records = MagicMock()
    research_records.list_for_company.return_value = []
    discovery_review = MagicMock()
    discovery_review.count_pending.return_value = 0
    discovery_merge_decisions = MagicMock()
    discovery_merge_decisions.get_latest.return_value = {
        "decision": "link",
        "company_id": str(COMPANY_ID),
    }
    discovery_merge_decisions.create.return_value = {"id": "decision-1"}

    service, conn = _service(
        companies=companies,
        source_records=source_records,
        research_records=research_records,
        discovery_review=discovery_review,
        discovery_merge_decisions=discovery_merge_decisions,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("app.discovery_reconcile_ops.audit_service.record_discovery_merge_decision", MagicMock())
        patcher.setattr("app.discovery_reconcile_ops.record_company_update_if_changed", MagicMock())
        service.commit_discovery_reconcile(
            conn,
            actor_context=ACTOR,
            candidates=[_candidate(domain=None, website=None)],
            run_id="run-1",
            merge_decisions=[
                {
                    "external_id": "yc:nimbus",
                    "decision": "link",
                    "company_id": str(COMPANY_ID),
                    "match_tier": "name",
                }
            ],
        )
        preview = service.preview_discovery_reconcile(
            conn,
            candidates=[_candidate(domain=None, website=None)],
        )

    assert preview["summary_counts"]["matched"] == 1
    assert preview["summary_counts"]["review"] == 0
