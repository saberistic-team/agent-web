"""Unit tests for discovery candidate reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.discovery.normalize import normalize_candidate
from app.discovery.observation import build_observation
from app.discovery.types import DiscoveryEvidence, DiscoveryObservation
from app.discovery_reconcile import (
    SOURCE_DISCOVERY,
    SOURCE_MANUAL,
    collect_domain_matches,
    compute_discovery_updates,
    domain_search_keys,
    is_field_user_owned,
    plan_evidence_sync,
    preview_candidate_row,
    resolve_company_match,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
RUN_ID = "run-preview-1"


def _company(**overrides: object) -> dict:
    base = {
        "id": COMPANY_ID,
        "name": "Nimbus Analytics",
        "domain": "nimbus.example.com",
        "website": "https://nimbus.example.com",
        "category": "ai_infrastructure",
        "notes": "Operator note",
        "field_sources": {
            "notes": {"source": SOURCE_MANUAL, "run_id": None, "seen_at": "2026-01-01T00:00:00+00:00"},
        },
    }
    base.update(overrides)
    return base


def _candidate(**overrides: object):
    base = normalize_candidate(
        source_id="yc",
        name="Nimbus Analytics",
        domain="nimbus.example.com",
        website="https://nimbus.example.com",
        signals=["ai infrastructure"],
        external_id="yc:nimbus",
    )
    if overrides:
        return normalize_candidate(
            source_id=str(overrides.pop("source_id", "yc")),
            name=str(overrides.pop("name", base.name)),
            domain=overrides.pop("domain", base.domain),
            website=overrides.pop("website", base.website),
            signals=list(overrides.pop("signals", base.signals)),
            external_id=str(overrides.pop("external_id", base.external_id)),
            observations=list(overrides.pop("observations", ())),
        )
    return base


def _observation(**overrides: object) -> DiscoveryObservation:
    base = build_observation(
        source_url="https://example.com/source",
        raw_source_id="obs-1",
        value="name=Nimbus Analytics",
        confidence=0.9,
        retrieved_at="2026-01-10T00:00:00+00:00",
    )
    if not overrides:
        return base
    return build_observation(
        source_url=str(overrides.get("source_url", base.source_url)),
        raw_source_id=str(overrides.get("raw_source_id", base.raw_source_id)),
        value=str(overrides.get("value", base.value)),
        confidence=float(overrides.get("confidence", base.confidence)),
        retrieved_at=str(overrides.get("retrieved_at", base.retrieved_at)),
    )


@pytest.mark.unit
def test_source_id_match_is_high_confidence() -> None:
    candidate = _candidate()
    company = _company()
    match = resolve_company_match(
        candidate,
        source_record={"company_id": str(COMPANY_ID)},
        companies_by_domain={},
        companies_by_name={},
        linked_company=company,
    )
    assert match.tier == "source_id"
    assert match.company == company


@pytest.mark.unit
def test_domain_match_requires_single_company() -> None:
    candidate = _candidate()
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={"nimbus.example.com": [_company()]},
        companies_by_name={},
    )
    assert match.tier == "domain"
    assert match.company is not None


@pytest.mark.unit
def test_domain_alias_matches_parent_host() -> None:
    candidate = _candidate(domain="app.nimbus.example.com")
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={"nimbus.example.com": [_company(domain="nimbus.example.com")]},
        companies_by_name={},
    )
    assert match.tier == "domain_alias"
    assert match.company is not None


@pytest.mark.unit
def test_domain_change_on_known_source_still_matches_source_id() -> None:
    candidate = _candidate(domain="new.nimbus.example.com", external_id="yc:nimbus")
    company = _company(domain="nimbus.example.com")
    match = resolve_company_match(
        candidate,
        source_record={"company_id": str(COMPANY_ID)},
        companies_by_domain={"new.nimbus.example.com": [_company(id=OTHER_COMPANY_ID, name="Other Co", domain="new.nimbus.example.com")]},
        companies_by_name={},
        linked_company=company,
    )
    assert match.tier == "source_id"
    assert match.company == company


@pytest.mark.unit
def test_name_only_match_requires_review() -> None:
    candidate = _candidate(domain=None, website=None)
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={},
        companies_by_name={"nimbus analytics": [_company(domain=None, website=None)]},
    )
    assert match.tier == "name"
    assert match.requires_review is True


@pytest.mark.unit
def test_similar_name_ambiguity_enters_conflict() -> None:
    candidate = _candidate(domain=None, website=None)
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={},
        companies_by_name={
            "nimbus analytics": [
                _company(id=COMPANY_ID),
                _company(id=OTHER_COMPANY_ID, name="Nimbus Analytics"),
            ]
        },
    )
    assert match.conflict is True
    assert len(match.candidates) == 2


@pytest.mark.unit
def test_duplicate_domain_enters_conflict() -> None:
    candidate = _candidate()
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={
            "nimbus.example.com": [
                _company(id=COMPANY_ID),
                _company(id=OTHER_COMPANY_ID, name="Nimbus Clone"),
            ]
        },
        companies_by_name={},
    )
    assert match.conflict is True


@pytest.mark.unit
def test_user_override_reuses_merge_decision() -> None:
    candidate = _candidate(domain=None, website=None)
    match = resolve_company_match(
        candidate,
        source_record=None,
        companies_by_domain={},
        companies_by_name={"nimbus analytics": [_company()]},
        merge_decision={"decision": "link", "company_id": str(COMPANY_ID)},
        linked_company=_company(),
    )
    assert match.tier == "override"
    assert match.requires_review is False


@pytest.mark.unit
def test_dismiss_decision_skips_candidate() -> None:
    candidate = _candidate()
    row = preview_candidate_row(
        row_index=0,
        candidate=candidate,
        match=resolve_company_match(
            candidate,
            source_record=None,
            companies_by_domain={"nimbus.example.com": [_company()]},
            companies_by_name={},
            merge_decision={"decision": "dismiss"},
        ),
        existing_records=[],
        run_id=RUN_ID,
    )
    assert row.outcome == "skipped"


@pytest.mark.unit
def test_compute_updates_respects_manual_notes_and_fills_empty_fields() -> None:
    company = _company(category=None)
    candidate = _candidate()
    updates, sources, changes = compute_discovery_updates(
        company,
        candidate,
        run_id=RUN_ID,
        seen_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    assert "notes" not in updates
    assert updates["category"] == "ai_infrastructure"
    assert sources["category"]["source"] == SOURCE_DISCOVERY
    assert any(change.field == "category" for change in changes)


@pytest.mark.unit
def test_manual_category_is_not_overwritten() -> None:
    company = _company(
        category="fintech",
        field_sources={
            "category": {"source": SOURCE_MANUAL, "run_id": None, "seen_at": "2026-01-01"},
        },
    )
    updates, _, changes = compute_discovery_updates(
        company,
        _candidate(signals=["ai infrastructure"]),
        run_id=RUN_ID,
    )
    assert "category" not in updates
    assert changes == []


@pytest.mark.unit
def test_repeated_observation_plans_refresh_not_duplicate() -> None:
    candidate = normalize_candidate(
        source_id="yc",
        name="Nimbus Analytics",
        domain="nimbus.example.com",
        external_id="yc:nimbus",
        observations=[_observation()],
    )
    existing = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "metadata": {"discovery_observation_key": "https://example.com/source|obs-1"},
        }
    ]
    plan = plan_evidence_sync(candidate, existing_records=existing)
    assert plan.append == []
    assert len(plan.refresh) == 1


@pytest.mark.unit
def test_new_observation_appends_evidence() -> None:
    candidate = normalize_candidate(
        source_id="yc",
        name="Nimbus Analytics",
        domain="nimbus.example.com",
        external_id="yc:nimbus",
        observations=[_observation(raw_source_id="obs-2")],
    )
    plan = plan_evidence_sync(candidate, existing_records=[])
    assert len(plan.append) == 1
    assert plan.refresh == []


@pytest.mark.unit
def test_matched_preview_counts_evidence_refresh() -> None:
    candidate = normalize_candidate(
        source_id="yc",
        name="Nimbus Analytics",
        domain="nimbus.example.com",
        external_id="yc:nimbus",
        observations=[_observation()],
    )
    row = preview_candidate_row(
        row_index=0,
        candidate=candidate,
        match=resolve_company_match(
            candidate,
            source_record={"company_id": str(COMPANY_ID)},
            companies_by_domain={},
            companies_by_name={},
            linked_company=_company(category="ai_infrastructure"),
        ),
        existing_records=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "metadata": {"discovery_observation_key": "https://example.com/source|obs-1"},
            }
        ],
        run_id=RUN_ID,
    )
    assert row.outcome == "matched"
    assert row.evidence_refresh_count == 1
    assert row.evidence_append_count == 0


@pytest.mark.unit
def test_domain_search_keys_include_parent_hosts() -> None:
    assert domain_search_keys("app.nimbus.example.com") == (
        "app.nimbus.example.com",
        "nimbus.example.com",
    )


@pytest.mark.unit
def test_collect_domain_matches_deduplicates_alias_hits() -> None:
    company = _company()
    matches, tier = collect_domain_matches(
        "app.nimbus.example.com",
        companies_by_domain={
            "nimbus.example.com": [company],
            "app.nimbus.example.com": [company],
        },
    )
    assert tier == "domain"
    assert len(matches) == 1


@pytest.mark.unit
def test_is_field_user_owned_blocks_manual_values() -> None:
    sources = {"notes": {"source": SOURCE_MANUAL}}
    assert is_field_user_owned(sources, "notes") is True
    assert is_field_user_owned(sources, "category") is False
