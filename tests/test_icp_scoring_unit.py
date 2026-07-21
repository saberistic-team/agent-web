"""Unit tests for deterministic ICP scoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.icp_scoring import (
    RULE_STATUS_DISABLED,
    RULE_STATUS_EXPIRED,
    RULE_STATUS_HYPOTHESIS,
    RULE_STATUS_MISSING,
    RULE_STATUS_SCORED,
    IcpRuleThreshold,
    IcpScoringRule,
    calculate_icp_score,
    default_icp_rules,
    rule_from_row,
    snapshot_from_result,
    validate_record_types,
    validate_rule_ids,
)

REFERENCE = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _company(**overrides: object) -> dict[str, object]:
    base = {
        "id": str(uuid4()),
        "name": "Acme",
        "category": "fintech",
        "stage": "seed",
        "headcount_estimate": 45,
        "funding_summary": "Raised seed in 2026",
        "target_status": "target",
        "pipeline_stage": "qualified",
        "last_verified_at": REFERENCE.date(),
    }
    base.update(overrides)
    return base


def _record(
    *,
    record_type: str = "verified_fact",
    observed_value: str = "Raised Series A funding",
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    observed = observed_at or (REFERENCE - timedelta(days=10))
    expires = expires_at or (REFERENCE + timedelta(days=60))
    return {
        "id": str(uuid4()),
        "record_type": record_type,
        "observed_value": observed_value,
        "body": observed_value,
        "source_name": "Crunchbase",
        "observed_at": observed.isoformat(),
        "expires_at": expires.isoformat(),
    }


@pytest.mark.unit
def test_default_rules_are_ten_point_set() -> None:
    rules = default_icp_rules()
    assert len(rules) == 10
    assert sum(rule.weight for rule in rules) == 10.0
    assert {rule.id for rule in rules} == {
        "vertical_fit",
        "stage_fit",
        "funding_recency",
        "hiring_growth",
        "technical_trigger",
        "warm_path",
        "decision_maker",
        "target_disposition",
        "pipeline_progress",
        "fresh_evidence",
    }


@pytest.mark.unit
def test_fully_populated_company_scores_deterministically() -> None:
    company = _company()
    contacts = [
        {
            "id": str(uuid4()),
            "full_name": "Alex Founder",
            "buying_roles": ["founder"],
        }
    ]
    records = [
        _record(observed_value="Raised seed funding round"),
        _record(observed_value="Hiring platform engineers"),
        _record(observed_value="Migrating payments API to new cloud platform"),
    ]
    rules = default_icp_rules()

    first = calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=records,
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )
    second = calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=records,
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )

    assert first == second
    assert first.total_score >= 7.0
    assert all(item.status == RULE_STATUS_SCORED for item in first.breakdown[:4])


@pytest.mark.unit
def test_missing_data_surfaces_missing_inputs() -> None:
    result = calculate_icp_score(
        company={"id": str(uuid4()), "name": "Sparse"},
        contacts=[],
        research_records=[],
        rules=default_icp_rules(),
        version_number=1,
        calculated_at=REFERENCE,
    )
    assert result.total_score == 0.0
    assert "company.category" in result.missing_inputs
    assert any(item.status == RULE_STATUS_MISSING for item in result.breakdown)


@pytest.mark.unit
def test_expired_evidence_does_not_count_without_hypothesis_rule() -> None:
    expired = _record(
        observed_value="Raised funding",
        observed_at=REFERENCE - timedelta(days=200),
        expires_at=REFERENCE - timedelta(days=1),
    )
    result = calculate_icp_score(
        company=_company(funding_summary=None, last_verified_at=None),
        contacts=[],
        research_records=[expired],
        rules=default_icp_rules(),
        version_number=1,
        calculated_at=REFERENCE,
    )
    funding = next(item for item in result.breakdown if item.rule_id == "funding_recency")
    fresh = next(item for item in result.breakdown if item.rule_id == "fresh_evidence")
    assert funding.points_awarded == 0.0
    assert funding.status == RULE_STATUS_EXPIRED
    assert fresh.status == RULE_STATUS_EXPIRED


@pytest.mark.unit
def test_hypothesis_only_evidence_is_rejected_by_default() -> None:
    hypothesis = {
        "id": str(uuid4()),
        "record_type": "hypothesis",
        "body": "Likely hiring platform engineers",
        "observed_value": "Hiring hypothesis",
    }
    result = calculate_icp_score(
        company=_company(headcount_estimate=None),
        contacts=[],
        research_records=[hypothesis],
        rules=default_icp_rules(),
        version_number=1,
        calculated_at=REFERENCE,
    )
    hiring = next(item for item in result.breakdown if item.rule_id == "hiring_growth")
    assert hiring.points_awarded == 0.0
    assert hiring.status == RULE_STATUS_HYPOTHESIS


@pytest.mark.unit
def test_hypothesis_counts_when_rule_allows_it() -> None:
    rules = default_icp_rules()
    hiring_rule = next(rule for rule in rules if rule.id == "hiring_growth")
    hiring_rule.accept_hypothesis = True
    hypothesis = {
        "id": str(uuid4()),
        "record_type": "hypothesis",
        "body": "Hiring platform engineers",
        "observed_value": "Hiring platform engineers",
    }
    result = calculate_icp_score(
        company=_company(headcount_estimate=None),
        contacts=[],
        research_records=[hypothesis],
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )
    hiring = next(item for item in result.breakdown if item.rule_id == "hiring_growth")
    assert hiring.points_awarded == 1.0
    assert hiring.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_manual_override_is_visually_distinct_in_result() -> None:
    result = calculate_icp_score(
        company=_company(),
        contacts=[],
        research_records=[],
        rules=default_icp_rules(),
        version_number=2,
        calculated_at=REFERENCE,
        is_override=True,
        override_reason="Partner intro confirmed",
        override_by="operator",
        override_score=8.5,
    )
    assert result.is_override is True
    assert result.total_score == 8.5
    assert result.computed_score < result.total_score
    assert result.override_reason == "Partner intro confirmed"


@pytest.mark.unit
def test_boundary_headcount_and_stage_thresholds() -> None:
    rules = [
        IcpScoringRule(
            id="stage_fit",
            dimension="stage",
            label="Stage",
            weight=1.0,
            threshold=IcpRuleThreshold(stages=["seed"]),
            sort_order=1,
        ),
        IcpScoringRule(
            id="hiring_growth",
            dimension="hiring_growth",
            label="Hiring",
            weight=1.0,
            threshold=IcpRuleThreshold(min_headcount=10, max_headcount=10),
            sort_order=2,
        ),
    ]
    inside = calculate_icp_score(
        company=_company(stage="seed", headcount_estimate=10),
        contacts=[],
        research_records=[],
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )
    outside = calculate_icp_score(
        company=_company(stage="public", headcount_estimate=9),
        contacts=[],
        research_records=[],
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )
    assert inside.total_score == 2.0
    assert outside.total_score == 0.0


@pytest.mark.unit
def test_disabled_rule_reports_disabled_status() -> None:
    rules = [
        IcpScoringRule(
            id="vertical_fit",
            dimension="vertical",
            label="Vertical",
            weight=1.0,
            threshold=IcpRuleThreshold(categories=["fintech"]),
            enabled=False,
            sort_order=1,
        )
    ]
    result = calculate_icp_score(
        company=_company(category="fintech"),
        contacts=[],
        research_records=[],
        rules=rules,
        version_number=1,
        calculated_at=REFERENCE,
    )
    assert result.breakdown[0].status == RULE_STATUS_DISABLED
    assert result.total_score == 0.0


@pytest.mark.unit
def test_warm_path_scores_introducer_contact() -> None:
    contacts = [
        {
            "id": str(uuid4()),
            "full_name": "Jordan Intro",
            "buying_roles": ["introducer"],
        }
    ]
    result = calculate_icp_score(
        company=_company(),
        contacts=contacts,
        research_records=[],
        rules=[rule for rule in default_icp_rules() if rule.id == "warm_path"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    warm = result.breakdown[0]
    assert warm.points_awarded == 1.0
    assert warm.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_funding_recency_uses_company_summary_when_recent() -> None:
    result = calculate_icp_score(
        company=_company(
            funding_summary="Raised seed in 2026",
            last_verified_at=REFERENCE.date(),
        ),
        contacts=[],
        research_records=[],
        rules=[rule for rule in default_icp_rules() if rule.id == "funding_recency"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    funding = result.breakdown[0]
    assert funding.points_awarded == 1.0
    assert funding.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_technical_trigger_matches_keyword_evidence() -> None:
    record = _record(observed_value="Migrating payments API infrastructure")
    result = calculate_icp_score(
        company=_company(),
        contacts=[],
        research_records=[record],
        rules=[rule for rule in default_icp_rules() if rule.id == "technical_trigger"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    technical = result.breakdown[0]
    assert technical.points_awarded == 1.0
    assert technical.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_expired_and_hypothesis_together_report_expired_status() -> None:
    expired = _record(
        observed_value="Hiring platform engineers",
        observed_at=REFERENCE - timedelta(days=200),
        expires_at=REFERENCE - timedelta(days=1),
    )
    hypothesis = {
        "id": str(uuid4()),
        "record_type": "hypothesis",
        "body": "Hiring hypothesis",
        "observed_value": "Hiring hypothesis",
    }
    result = calculate_icp_score(
        company=_company(headcount_estimate=None),
        contacts=[],
        research_records=[expired, hypothesis],
        rules=[rule for rule in default_icp_rules() if rule.id == "hiring_growth"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    hiring = result.breakdown[0]
    assert hiring.points_awarded == 0.0
    assert hiring.status == RULE_STATUS_EXPIRED


@pytest.mark.unit
def test_rule_from_row_and_snapshot_helpers() -> None:
    row = {
        "id": "vertical_fit",
        "dimension": "vertical",
        "label": "Target vertical",
        "weight": 1.0,
        "threshold": {"categories": ["fintech"]},
        "enabled": True,
        "accept_hypothesis": False,
        "sort_order": 1,
    }
    rule = rule_from_row(row)
    assert rule.id == "vertical_fit"

    result = calculate_icp_score(
        company=_company(category="fintech"),
        contacts=[],
        research_records=[],
        rules=[rule],
        version_number=1,
        calculated_at=REFERENCE,
    )
    snapshot = snapshot_from_result(
        company_id=str(uuid4()),
        version_id=str(uuid4()),
        result=result,
    )
    assert snapshot["total_score"] == 1.0
    assert snapshot["version_number"] == 1


@pytest.mark.unit
def test_validate_rule_and_record_type_helpers() -> None:
    validate_rule_ids([rule.id for rule in default_icp_rules()])
    with pytest.raises(ValueError, match="canonical ten"):
        validate_rule_ids(["vertical_fit"])
    validate_record_types(["verified_fact", "hypothesis"])
    with pytest.raises(ValueError, match="unknown record_type"):
        validate_record_types(["not_a_type"])


@pytest.mark.unit
def test_threshold_validator_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        IcpRuleThreshold(max_days=-1)


@pytest.mark.unit
def test_invalid_dimension_is_rejected() -> None:
    with pytest.raises(ValueError, match="dimension must be one of"):
        IcpScoringRule(
            id="bad_rule",
            dimension="not_a_dimension",
            label="Bad",
            weight=1.0,
        )


@pytest.mark.unit
def test_warm_path_scores_relationship_context_record() -> None:
    record = {
        "id": str(uuid4()),
        "record_type": "relationship_context",
        "body": "Warm intro via portfolio founder",
        "observed_value": "Warm intro via portfolio founder",
    }
    result = calculate_icp_score(
        company=_company(),
        contacts=[],
        research_records=[record],
        rules=[rule for rule in default_icp_rules() if rule.id == "warm_path"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    warm = result.breakdown[0]
    assert warm.points_awarded == 1.0
    assert warm.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_hiring_growth_matches_research_record_keywords() -> None:
    record = _record(observed_value="Actively hiring platform engineers")
    result = calculate_icp_score(
        company=_company(headcount_estimate=None),
        contacts=[],
        research_records=[record],
        rules=[rule for rule in default_icp_rules() if rule.id == "hiring_growth"],
        version_number=1,
        calculated_at=REFERENCE,
    )
    hiring = result.breakdown[0]
    assert hiring.points_awarded == 1.0
    assert hiring.status == RULE_STATUS_SCORED


@pytest.mark.unit
def test_rule_from_row_accepts_non_dict_threshold() -> None:
    rule = rule_from_row(
        {
            "id": "vertical_fit",
            "dimension": "vertical",
            "label": "Target vertical",
            "weight": 1.0,
            "threshold": "invalid",
        }
    )
    assert rule.threshold.categories == []
