"""Unit tests for qualification target service methods."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.crm_service import CrmRepositories, CrmService
from app.icp_scoring import default_icp_rules
from app.qualification_targets import QualificationTargetFilters, WorkingListCreate

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
VERSION_ID = UUID("99999999-9999-9999-9999-999999999901")


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


@pytest.mark.unit
def test_list_qualification_targets_returns_empty_without_active_version() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.icp_scoring.get_active_version.return_value = None
    assert service.list_qualification_targets(conn) == []


@pytest.mark.unit
def test_list_qualification_targets_builds_rows_and_records_tier_change() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    service._repos.qualification.list_active_companies.return_value = [
        {
            "id": str(COMPANY_ID),
            "name": "Acme",
            "category": "fintech",
            "stage": "seed",
            "target_status": "target",
            "pipeline_stage": "qualified",
            "headcount_estimate": 40,
            "last_verified_at": datetime(2026, 7, 1, tzinfo=timezone.utc).date(),
        }
    ]
    service._repos.contacts.list_for_company.return_value = [
        {"id": str(UUID(int=2)), "full_name": "Alex", "buying_roles": ["founder"]}
    ]
    service._repos.research_records.list_for_company.return_value = []
    service._repos.icp_scoring.insert_snapshot.return_value = {"id": str(UUID(int=3))}
    service._repos.qualification.get_latest_tier_for_company.return_value = None

    rows = service.list_qualification_targets(
        conn,
        actor="operator",
        persist_scores=True,
    )

    assert len(rows) >= 1
    assert rows[0]["tier"] in {"A", "B", "C"}
    service._repos.qualification.record_tier_change.assert_called_once()


@pytest.mark.unit
def test_list_qualification_targets_skips_tier_change_when_unchanged() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    service._repos.qualification.list_active_companies.return_value = [
        {
            "id": str(COMPANY_ID),
            "name": "Acme",
            "category": "fintech",
            "stage": "seed",
            "target_status": "target",
            "pipeline_stage": "qualified",
            "headcount_estimate": 40,
            "last_verified_at": datetime(2026, 7, 1, tzinfo=timezone.utc).date(),
        }
    ]
    service._repos.contacts.list_for_company.return_value = []
    service._repos.research_records.list_for_company.return_value = []
    service._repos.icp_scoring.insert_snapshot.return_value = {"id": str(UUID(int=3))}

    rows = service.list_qualification_targets(conn, actor="operator", persist_scores=True)
    assert rows
    current_tier = rows[0]["tier"]
    service._repos.qualification.get_latest_tier_for_company.return_value = current_tier
    service._repos.qualification.record_tier_change.reset_mock()

    service.list_qualification_targets(conn, actor="operator", persist_scores=True)
    service._repos.qualification.record_tier_change.assert_not_called()


@pytest.mark.unit
def test_list_qualification_targets_applies_filters() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.icp_scoring.get_active_version.return_value = {
        "id": VERSION_ID,
        "version_number": 1,
    }
    service._repos.icp_scoring.list_rules_for_version.return_value = [
        rule.model_dump() for rule in default_icp_rules()
    ]
    service._repos.qualification.list_active_companies.return_value = [
        {
            "id": str(COMPANY_ID),
            "name": "Acme",
            "category": "fintech",
            "stage": "seed",
            "target_status": "target",
            "pipeline_stage": "qualified",
            "headcount_estimate": 40,
            "last_verified_at": datetime(2026, 7, 1, tzinfo=timezone.utc).date(),
        },
        {
            "id": str(UUID(int=4)),
            "name": "Beta",
            "category": "other",
            "stage": "series_a",
            "target_status": "target",
            "pipeline_stage": "researching",
            "headcount_estimate": 10,
            "last_verified_at": datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
        },
    ]
    service._repos.contacts.list_for_company.return_value = []
    service._repos.research_records.list_for_company.return_value = []

    rows = service.list_qualification_targets(
        conn,
        filters=QualificationTargetFilters(tier="A"),
        persist_scores=False,
    )
    assert all(row["tier"] == "A" for row in rows)


@pytest.mark.unit
def test_save_qualification_working_list_delegates_to_repository() -> None:
    conn = MagicMock()
    service = _service()
    payload = WorkingListCreate(name="Shortlist", company_ids=[str(COMPANY_ID)])
    service._repos.qualification.create_working_list.return_value = {"id": str(UUID(int=9))}

    result = service.save_qualification_working_list(
        conn,
        owner="operator",
        payload=payload,
    )

    assert result["id"] == str(UUID(int=9))
    kwargs = service._repos.qualification.create_working_list.call_args.kwargs
    assert kwargs["owner"] == "operator"
    assert kwargs["company_ids"] == [COMPANY_ID]


@pytest.mark.unit
def test_list_qualification_tier_history_delegates() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.qualification.list_tier_history.return_value = [{"to_tier": "A"}]
    rows = service.list_qualification_tier_history(conn, COMPANY_ID)
    assert rows[0]["to_tier"] == "A"


@pytest.mark.unit
def test_list_qualification_working_lists_delegates() -> None:
    conn = MagicMock()
    service = _service()
    service._repos.qualification.list_working_lists_for_owner.return_value = [{"name": "Q3"}]
    rows = service.list_qualification_working_lists(conn, owner="operator")
    assert rows[0]["name"] == "Q3"


@pytest.mark.unit
def test_get_qualification_working_list_items_delegates() -> None:
    conn = MagicMock()
    service = _service()
    list_id = UUID(int=8)
    service._repos.qualification.get_working_list_items.return_value = [
        {"company_id": str(COMPANY_ID)}
    ]
    rows = service.get_qualification_working_list_items(conn, list_id)
    assert rows[0]["company_id"] == str(COMPANY_ID)
