"""Integration tests for deterministic ICP scoring across engine and service layers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService
from app.icp_scoring import (
    RULE_STATUS_EXPIRED,
    RULE_STATUS_MISSING,
    RULE_STATUS_SCORED,
    IcpRuleThreshold,
    IcpScoringRule,
    calculate_icp_score,
    default_icp_rules,
    rule_from_row,
    snapshot_from_result,
)
from app.repositories.postgres import PostgresIcpScoringRepository
from app.research_records import (
    format_record_timestamp,
    safe_source_link,
    validate_source_url,
)

REFERENCE = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
VERSION_ID = UUID("99999999-9999-9999-9999-999999999901")


def _actor() -> ActorContext:
    return ActorContext(actor="operator", correlation_id="corr-icp-integration")


def _company(**overrides: object) -> dict[str, object]:
    base = {
        "id": str(COMPANY_ID),
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


def _service() -> CrmService:
    return CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )


@pytest.mark.integration
def test_calculate_icp_score_full_company_end_to_end() -> None:
    company = _company()
    contacts = [{"id": str(uuid4()), "full_name": "Alex", "buying_roles": ["founder"]}]
    records = [
        _record(observed_value="Raised seed funding round"),
        _record(observed_value="Hiring platform engineers"),
        _record(observed_value="Migrating payments API to new cloud platform"),
        _record(record_type="relationship_context", observed_value="Intro from partner"),
    ]
    result = calculate_icp_score(
        company=company,
        contacts=contacts,
        research_records=records,
        rules=default_icp_rules(),
        version_number=1,
        calculated_at=REFERENCE,
    )
    assert result.total_score == result.computed_score
    assert result.total_score >= 8.0
    assert all(item.status == RULE_STATUS_SCORED for item in result.breakdown if item.points_awarded)
    snapshot = snapshot_from_result(
        company_id=str(COMPANY_ID),
        version_id=str(VERSION_ID),
        result=result,
    )
    assert snapshot["total_score"] == result.total_score
    assert len(snapshot["breakdown"]) == 10


@pytest.mark.integration
def test_calculate_icp_score_expired_and_hypothesis_evidence() -> None:
    company = _company(
        category=None,
        stage=None,
        funding_summary=None,
        last_verified_at=None,
        headcount_estimate=None,
    )
    expired = _record(
        observed_value="Old funding round",
        observed_at=REFERENCE - timedelta(days=200),
        expires_at=REFERENCE - timedelta(days=1),
    )
    hypothesis = {
        "id": str(uuid4()),
        "record_type": "hypothesis",
        "body": "May be hiring",
        "observed_value": "May be hiring",
    }
    result = calculate_icp_score(
        company=company,
        contacts=[],
        research_records=[expired, hypothesis],
        rules=default_icp_rules(),
        version_number=1,
        calculated_at=REFERENCE,
    )
    funding = next(item for item in result.breakdown if item.rule_id == "funding_recency")
    assert funding.status == RULE_STATUS_EXPIRED
    vertical = next(item for item in result.breakdown if item.rule_id == "vertical_fit")
    assert vertical.status == RULE_STATUS_MISSING
    hiring = next(item for item in result.breakdown if item.rule_id == "hiring_growth")
    assert hiring.status == RULE_STATUS_EXPIRED


@pytest.mark.integration
def test_calculate_icp_score_hypothesis_accepted_when_rule_allows() -> None:
    company = _company()
    hypothesis = _record(record_type="hypothesis", observed_value="Raised seed round")
    rules = default_icp_rules()
    funding_rule = next(rule for rule in rules if rule.id == "funding_recency")
    funding_rule = funding_rule.model_copy(update={"accept_hypothesis": True})
    updated_rules = [funding_rule if rule.id == "funding_recency" else rule for rule in rules]
    result = calculate_icp_score(
        company=company,
        contacts=[],
        research_records=[hypothesis],
        rules=updated_rules,
        version_number=2,
        calculated_at=REFERENCE,
    )
    funding = next(item for item in result.breakdown if item.rule_id == "funding_recency")
    assert funding.status == RULE_STATUS_SCORED
    assert funding.points_awarded == 1.0


@pytest.mark.integration
def test_calculate_icp_score_disabled_rule_and_custom_thresholds() -> None:
    rules = default_icp_rules()
    disabled = rules[0].model_copy(update={"enabled": False})
    custom = IcpScoringRule(
        id="warm_path",
        dimension="warm_path",
        label="Custom warm path",
        weight=2.0,
        threshold=IcpRuleThreshold(record_types=["relationship_context"]),
        sort_order=6,
    )
    updated = [disabled if rule.id == disabled.id else rule for rule in rules]
    updated = [custom if rule.id == "warm_path" else rule for rule in updated]
    result = calculate_icp_score(
        company=_company(),
        contacts=[],
        research_records=[_record(record_type="relationship_context", observed_value="Warm intro")],
        rules=updated,
        version_number=3,
        calculated_at=REFERENCE,
    )
    vertical = next(item for item in result.breakdown if item.rule_id == disabled.id)
    assert vertical.status == "disabled"
    warm = next(item for item in result.breakdown if item.rule_id == "warm_path")
    assert warm.points_awarded == 2.0


@pytest.mark.integration
def test_crm_service_calculate_and_override_company_score() -> None:
    conn = MagicMock()
    service = _service()
    company = _company()
    service._repos.companies.get_by_id.return_value = company
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    service._repos.contacts.list_for_company.return_value = [
        {"id": str(uuid4()), "full_name": "Alex", "buying_roles": ["founder"]}
    ]
    service._repos.research_records.list_for_company.return_value = [
        _record(observed_value="Raised seed funding round")
    ]
    service._repos.icp_scoring.insert_snapshot.return_value = {"id": str(uuid4())}

    calculated = service.calculate_company_icp_score(
        conn,
        actor_context=_actor(),
        company_id=COMPANY_ID,
        calculated_at=REFERENCE,
    )
    assert calculated["result"].total_score >= 1.0
    service._repos.icp_scoring.insert_snapshot.assert_called_once()

    prior_snapshot = {
        "id": str(uuid4()),
        "company_id": str(COMPANY_ID),
        "version_id": str(VERSION_ID),
        "version_number": 1,
        "total_score": calculated["result"].total_score,
        "computed_score": calculated["result"].computed_score,
        "breakdown": [item.model_dump() for item in calculated["result"].breakdown],
        "missing_inputs": calculated["result"].missing_inputs,
        "calculated_at": REFERENCE,
        "is_override": False,
    }
    service._repos.icp_scoring.get_latest_snapshot_for_company.return_value = prior_snapshot
    service._repos.icp_scoring.insert_snapshot.return_value = {
        "id": str(uuid4()),
        "is_override": True,
        "total_score": 9.5,
    }
    override = service.override_company_icp_score(
        conn,
        actor_context=_actor(),
        company_id=COMPANY_ID,
        override_score=9.5,
        reason="Strategic partner referral",
        calculated_at=REFERENCE,
    )
    assert override["snapshot"]["is_override"] is True
    override_kwargs = service._repos.icp_scoring.insert_snapshot.call_args.kwargs
    assert override_kwargs["is_override"] is True
    assert override_kwargs["override_reason"] == "Strategic partner referral"


@pytest.mark.integration
def test_crm_service_publish_icp_rule_version_audits_changes() -> None:
    conn = MagicMock()
    service = _service()
    active_version = {
        "id": VERSION_ID,
        "version_number": 1,
        "label": "Default",
        "is_active": True,
    }
    current_rules = [rule.model_dump() for rule in default_icp_rules()]
    updated_rules = default_icp_rules()
    updated_rules[0] = updated_rules[0].model_copy(update={"weight": 1.5})

    service._repos.icp_scoring.get_active_version.return_value = active_version
    service._repos.icp_scoring.list_rules_for_version.return_value = current_rules
    service._repos.icp_scoring.create_version.return_value = {
        "id": uuid4(),
        "version_number": 2,
        "label": "ICP rules v2",
        "is_active": True,
    }
    service._repos.icp_scoring.insert_rule.side_effect = lambda *args, **kwargs: kwargs

    with patch("app.crm_service.audit_service.record_scoring_rule_update") as audit:
        result = service.publish_icp_rule_version(
            conn,
            actor_context=_actor(),
            rules=updated_rules,
        )

    assert result["version"]["version_number"] == 2
    audit.assert_called()
    service._repos.icp_scoring.deactivate_all_versions.assert_called_once_with(conn)


@pytest.mark.integration
def test_crm_service_icp_list_and_detail_helpers() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.icp_scoring.get_active_version.return_value = None
    assert service.list_active_icp_rules(conn) == []
    assert service.get_active_icp_version(conn) is None

    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    rules = service.list_active_icp_rules(conn)
    assert len(rules) == 10

    service._repos.companies.get_by_id.return_value = None
    assert service.get_company_icp_score_detail(conn, COMPANY_ID) is None

    service._repos.icp_scoring.list_latest_snapshots.return_value = [{"company_name": "Acme"}]
    rows = service.list_company_icp_scores(conn, limit=3)
    assert rows[0]["company_name"] == "Acme"


@pytest.mark.integration
def test_rule_from_row_round_trip() -> None:
    row = {
        "id": "vertical_fit",
        "dimension": "vertical",
        "label": "Target vertical",
        "weight": 1.0,
        "threshold": {"categories": ["fintech", "ai_infrastructure"]},
        "enabled": True,
        "accept_hypothesis": False,
        "sort_order": 1,
    }
    rule = rule_from_row(row)
    assert rule.id == "vertical_fit"
    assert rule.threshold.categories == ["fintech", "ai_infrastructure"]


@pytest.mark.integration
def test_icp_scoring_repository_list_snapshots_with_mock_conn() -> None:
    repo = PostgresIcpScoringRepository()
    row = {
        "id": UUID("88888888-8888-8888-8888-888888888881"),
        "company_id": COMPANY_ID,
        "company_name": "Acme",
        "version_number": 1,
        "total_score": 7.0,
        "computed_score": 7.0,
        "calculated_at": REFERENCE,
        "is_override": False,
    }
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [row]
    snapshots = repo.list_latest_snapshots(conn, limit=5)
    assert len(snapshots) == 1
    assert snapshots[0]["company_name"] == "Acme"
    sql = str(cur.execute.call_args.args[0])
    assert "company_icp_score_snapshots" in sql


@pytest.mark.integration
def test_research_record_helpers_for_icp_evidence_rendering() -> None:
    assert validate_source_url("https://example.com/report") == "https://example.com/report"
    with pytest.raises(ValueError, match="http or https"):
        validate_source_url("ftp://example.com")
    with pytest.raises(ValueError, match="must not be empty"):
        validate_source_url("   ")

    link = safe_source_link("https://example.com/report", label="Annual report")
    assert "Annual report" in link
    assert 'href="https://example.com/report"' in link

    formatted = format_record_timestamp(REFERENCE)
    assert "2026-07-15" in formatted
    assert format_record_timestamp(None) == ""
    assert format_record_timestamp(REFERENCE.isoformat()) == formatted
