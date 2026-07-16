"""Unit tests for ICP scoring service methods."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService
from app.icp_scoring import default_icp_rules

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
VERSION_ID = UUID("99999999-9999-9999-9999-999999999901")


def _actor() -> ActorContext:
    return ActorContext(actor="operator", correlation_id="corr-icp")


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
        )
    )


@pytest.mark.unit
def test_publish_icp_rule_version_creates_new_version_and_audits_changes() -> None:
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

    service._repos.icp_scoring.deactivate_all_versions.assert_called_once_with(conn)
    service._repos.icp_scoring.create_version.assert_called_once()
    assert result["version"]["version_number"] == 2
    audit.assert_called()


@pytest.mark.unit
def test_calculate_company_icp_score_persists_snapshot() -> None:
    conn = MagicMock()
    service = _service()
    company = {
        "id": str(COMPANY_ID),
        "name": "Acme",
        "category": "fintech",
        "stage": "seed",
        "target_status": "target",
        "pipeline_stage": "qualified",
        "headcount_estimate": 40,
        "last_verified_at": datetime(2026, 7, 1, tzinfo=timezone.utc).date(),
        "funding_summary": "Seed round",
    }
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
    service._repos.research_records.list_for_company.return_value = []
    service._repos.icp_scoring.insert_snapshot.return_value = {"id": str(uuid4())}

    result = service.calculate_company_icp_score(
        conn,
        actor_context=_actor(),
        company_id=COMPANY_ID,
        calculated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )

    service._repos.icp_scoring.insert_snapshot.assert_called_once()
    assert result["result"].version_number == 1
    assert result["result"].total_score > 0


@pytest.mark.unit
def test_override_requires_reason_and_persists_override_snapshot() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.companies.get_by_id.return_value = {
        "id": str(COMPANY_ID),
        "name": "Acme",
    }
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    service._repos.contacts.list_for_company.return_value = []
    service._repos.research_records.list_for_company.return_value = []
    service._repos.icp_scoring.insert_snapshot.return_value = {"id": str(uuid4())}

    with pytest.raises(ValueError, match="reason"):
        service.override_company_icp_score(
            conn,
            actor_context=_actor(),
            company_id=COMPANY_ID,
            override_score=8.0,
            reason="   ",
        )

    result = service.override_company_icp_score(
        conn,
        actor_context=_actor(),
        company_id=COMPANY_ID,
        override_score=8.0,
        reason="Partner intro confirmed",
    )
    kwargs = service._repos.icp_scoring.insert_snapshot.call_args.kwargs
    assert kwargs["is_override"] is True
    assert kwargs["override_reason"] == "Partner intro confirmed"
    assert result["result"].is_override is True


@pytest.mark.unit
def test_historical_snapshots_are_not_rewritten_on_rule_publish() -> None:
    conn = MagicMock()
    service = _service()
    snapshot = {
        "id": str(uuid4()),
        "company_id": str(COMPANY_ID),
        "version_number": 1,
        "total_score": 6.0,
    }
    service._repos.companies.get_by_id.return_value = {"id": str(COMPANY_ID), "name": "Acme"}
    service._repos.icp_scoring.get_latest_snapshot_for_company.return_value = snapshot
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 2,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]

    detail = service.get_company_icp_score_detail(conn, COMPANY_ID)
    assert detail is not None
    assert detail["snapshot"]["version_number"] == 1
    assert detail["active_version"]["version_number"] == 2
