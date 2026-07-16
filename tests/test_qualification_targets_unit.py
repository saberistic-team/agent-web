"""Unit tests for qualification tier target lists."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.icp_scoring import (
    IcpScoreResult,
    RuleContribution,
    RULE_STATUS_SCORED,
    calculate_icp_score,
    default_icp_rules,
)
from app.qualification_targets import (
    MAX_WORKING_LIST_ITEMS,
    QualificationTargetFilters,
    QualificationTargetRow,
    WorkingListCreate,
    build_target_row,
    filter_target_rows,
    is_active_target_score,
    sort_target_rows,
    tier_for_score,
)


COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")


def _score_result(total: float, *, missing: list[str] | None = None) -> IcpScoreResult:
    return IcpScoreResult(
        version_number=1,
        total_score=total,
        computed_score=total,
        breakdown=[],
        missing_inputs=missing or [],
        calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )


@pytest.mark.unit
def test_tier_thresholds_exclude_below_four() -> None:
    assert tier_for_score(10) == "A"
    assert tier_for_score(8) == "A"
    assert tier_for_score(7) == "B"
    assert tier_for_score(6) == "B"
    assert tier_for_score(5) == "C"
    assert tier_for_score(4) == "C"
    assert tier_for_score(3.9) is None
    assert tier_for_score(0) is None
    assert not is_active_target_score(3)
    assert is_active_target_score(4)


@pytest.mark.unit
def test_deterministic_sort_uses_visible_tie_breakers() -> None:
    rows = [
        QualificationTargetRow(
            company_id="b-id",
            name="Beta",
            score=8.0,
            tier="A",
            stage="seed",
            vertical="fintech",
            strongest_signals=("Target vertical",),
            warm_path=None,
            has_warm_path=False,
            next_action=None,
            evidence_freshness="fresh",
            missing_fields=(),
            pipeline_stage=None,
            pipeline_owner=None,
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Beta",
            stale_evidence=False,
        ),
        QualificationTargetRow(
            company_id="a-id",
            name="Alpha",
            score=8.0,
            tier="A",
            stage="seed",
            vertical="fintech",
            strongest_signals=("Target vertical",),
            warm_path=None,
            has_warm_path=False,
            next_action=None,
            evidence_freshness="fresh",
            missing_fields=(),
            pipeline_stage=None,
            pipeline_owner=None,
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Alpha",
            stale_evidence=False,
        ),
        QualificationTargetRow(
            company_id="c-id",
            name="Charlie",
            score=7.0,
            tier="B",
            stage="seed",
            vertical="fintech",
            strongest_signals=(),
            warm_path=None,
            has_warm_path=False,
            next_action=None,
            evidence_freshness="fresh",
            missing_fields=(),
            pipeline_stage=None,
            pipeline_owner=None,
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Charlie",
            stale_evidence=False,
        ),
    ]
    sorted_rows = sort_target_rows(rows)
    assert [row.name for row in sorted_rows] == ["Alpha", "Beta", "Charlie"]


@pytest.mark.unit
def test_filters_cover_tier_category_stage_pipeline_owner_freshness_warm_path() -> None:
    rows = [
        QualificationTargetRow(
            company_id="1",
            name="Acme",
            score=9.0,
            tier="A",
            stage="seed",
            vertical="fintech",
            strongest_signals=(),
            warm_path="Sam Intro",
            has_warm_path=True,
            next_action="Call",
            evidence_freshness="fresh",
            missing_fields=(),
            pipeline_stage="qualified",
            pipeline_owner="Alex",
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Acme",
            stale_evidence=False,
        ),
        QualificationTargetRow(
            company_id="2",
            name="Beta",
            score=6.0,
            tier="B",
            stage="series_a",
            vertical="other",
            strongest_signals=(),
            warm_path=None,
            has_warm_path=False,
            next_action=None,
            evidence_freshness="stale",
            missing_fields=("company.stage",),
            pipeline_stage="researching",
            pipeline_owner="Sam",
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Beta",
            stale_evidence=True,
        ),
    ]
    filters = QualificationTargetFilters(
        tier="A",
        category="fintech",
        stage="seed",
        pipeline_stage="qualified",
        owner="alex",
        freshness="fresh",
        warm_path="yes",
    )
    assert len(filter_target_rows(rows, filters)) == 1
    assert filter_target_rows(rows, QualificationTargetFilters(warm_path="no"))[0].name == "Beta"


@pytest.mark.unit
def test_build_target_row_surfaces_stale_evidence_and_missing_fields() -> None:
    company = {
        "id": COMPANY_ID,
        "name": "Stale Co",
        "category": "fintech",
        "stage": "seed",
        "last_verified_at": date(2025, 1, 1),
        "next_action": "Verify funding",
        "pipeline_stage": "researching",
        "pipeline_owner": "alex",
    }
    result = IcpScoreResult(
        version_number=1,
        total_score=6.0,
        computed_score=6.0,
        breakdown=[
            RuleContribution(
                rule_id="funding_recency",
                dimension="funding_recency",
                label="Recent funding signal",
                weight=1.0,
                points_awarded=0.0,
                status="expired_only",
                missing_inputs=[],
            )
        ],
        missing_inputs=["company.headcount_estimate"],
        calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    row = build_target_row(company=company, score_result=result)
    assert row is not None
    assert row.tier == "B"
    assert row.stale_evidence is True
    assert "company.headcount_estimate" in row.missing_fields
    assert build_target_row(company=company, score_result=_score_result(2.0)) is None


@pytest.mark.unit
def test_working_list_is_bounded_and_stores_ids_only() -> None:
    payload = WorkingListCreate(
        name="Shortlist",
        company_ids=[str(UUID(int=i)) for i in range(3)],
    )
    assert payload.name == "Shortlist"
    with pytest.raises(ValidationError):
        WorkingListCreate(
            name="Too big",
            company_ids=[str(UUID(int=i)) for i in range(MAX_WORKING_LIST_ITEMS + 1)],
        )


@pytest.mark.unit
def test_icp_score_is_deterministic_for_same_inputs() -> None:
    company = {
        "id": COMPANY_ID,
        "name": "Acme",
        "category": "fintech",
        "stage": "seed",
        "target_status": "target",
        "headcount_estimate": 40,
        "last_verified_at": date(2026, 6, 1),
        "pipeline_stage": "qualified",
    }
    contacts = [
        {
            "id": UUID("22222222-2222-2222-2222-222222222222"),
            "full_name": "Alex Founder",
            "buying_roles": ["founder"],
        }
    ]
    research = [
        {
            "id": UUID("33333333-3333-3333-3333-333333333333"),
            "record_type": "verified_fact",
            "observed_value": "Raised seed funding",
            "source_name": "Crunchbase",
            "observed_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "expires_at": datetime(2027, 5, 1, tzinfo=timezone.utc),
        }
    ]
    rules = default_icp_rules()
    reference = datetime(2026, 7, 16, tzinfo=timezone.utc)
    first = calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=research,
        rules=rules,
        version_number=1,
        calculated_at=reference,
    )
    second = calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=research,
        rules=rules,
        version_number=1,
        calculated_at=reference,
    )
    assert first.total_score == second.total_score
    assert tier_for_score(first.total_score) in {"A", "B", "C"}


@pytest.mark.unit
def test_filter_validators_reject_unknown_values() -> None:
    with pytest.raises(ValidationError):
        QualificationTargetFilters(tier="Z")
    with pytest.raises(ValidationError):
        QualificationTargetFilters(category="unknown_category")
    with pytest.raises(ValidationError):
        QualificationTargetFilters(stage="unknown_stage")
    with pytest.raises(ValidationError):
        QualificationTargetFilters(pipeline_stage="unknown_pipeline")
    with pytest.raises(ValidationError):
        QualificationTargetFilters(freshness="bad")
    with pytest.raises(ValidationError):
        QualificationTargetFilters(warm_path="maybe")


@pytest.mark.unit
def test_company_freshness_state_handles_dates_and_unknown() -> None:
    from app.qualification_targets import company_freshness_state

    reference = date(2026, 7, 16)
    assert company_freshness_state({}, reference=reference) == "unknown"
    assert (
        company_freshness_state(
            {"last_verified_at": datetime(2026, 7, 10, tzinfo=timezone.utc)},
            reference=reference,
        )
        == "fresh"
    )
    assert (
        company_freshness_state(
            {"last_verified_at": date(2026, 3, 1)},
            reference=reference,
        )
        == "stale"
    )
    assert (
        company_freshness_state(
            {"last_verified_at": date(2026, 5, 1)},
            reference=reference,
        )
        == "mixed"
    )
    assert company_freshness_state({"last_verified_at": "invalid"}, reference=reference) == "unknown"


@pytest.mark.unit
def test_build_target_row_warm_path_and_stale_freshness_adjustments() -> None:
    company = {
        "id": COMPANY_ID,
        "name": "Warm Co",
        "category": "fintech",
        "stage": "seed",
        "last_verified_at": None,
    }
    result = IcpScoreResult(
        version_number=1,
        total_score=8.0,
        computed_score=8.0,
        breakdown=[
            RuleContribution(
                rule_id="warm_path",
                dimension="warm_path",
                label="Warm path",
                weight=1.0,
                points_awarded=1.0,
                status=RULE_STATUS_SCORED,
                missing_inputs=[],
                evidence=[{"kind": "contact", "full_name": "Sam", "buying_roles": ["introducer"]}],
            ),
            RuleContribution(
                rule_id="funding_recency",
                dimension="funding_recency",
                label="Recent funding",
                weight=1.0,
                points_awarded=0.0,
                status="expired_only",
                missing_inputs=[],
            ),
        ],
        missing_inputs=[],
        calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    row = build_target_row(company=company, score_result=result)
    assert row is not None
    assert row.has_warm_path is True
    assert "Sam" in (row.warm_path or "")
    assert row.evidence_freshness == "stale"
    assert row.stale_evidence is True


@pytest.mark.unit
def test_filter_target_rows_freshness_unknown_and_owner_matching() -> None:
    rows = [
        QualificationTargetRow(
            company_id="1",
            name="Unknown Fresh",
            score=6.0,
            tier="B",
            stage="seed",
            vertical="fintech",
            strongest_signals=(),
            warm_path=None,
            has_warm_path=False,
            next_action=None,
            evidence_freshness="unknown",
            missing_fields=(),
            pipeline_stage="qualified",
            pipeline_owner="Alex",
            score_calculated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
            tie_breaker_name="Unknown Fresh",
            stale_evidence=False,
        )
    ]
    assert len(filter_target_rows(rows, QualificationTargetFilters(freshness="unknown"))) == 1
    assert len(filter_target_rows(rows, QualificationTargetFilters(owner="alex"))) == 1
    assert len(filter_target_rows(rows, QualificationTargetFilters(owner="sam"))) == 0


@pytest.mark.unit
def test_tier_change_metadata_includes_score_and_tiers() -> None:
    from app.qualification_targets import tier_change_metadata

    meta = tier_change_metadata(previous_tier="B", new_tier="A", score=8.5)
    assert meta["previous_tier"] == "B"
    assert meta["new_tier"] == "A"
    assert meta["score"] == 8.5
    assert "recorded_at" in meta
