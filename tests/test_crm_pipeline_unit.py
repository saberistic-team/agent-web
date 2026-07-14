"""Unit tests for acquisition pipeline CRM service and repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.crm_service import CrmRepositories, CrmService
from app.pipeline import ConfirmRequiredError, ReasonRequiredError
from app.repositories.postgres import (
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresStageHistoryRepository,
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
HISTORY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
AUDIT_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _mock_conn(row: dict | list | None = None) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    if isinstance(row, list):
        cur.fetchall.return_value = row
    elif row is not None:
        cur.fetchone.return_value = row
    return conn


def _service_with_mocks() -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    repos = {
        "companies": MagicMock(),
        "contacts": MagicMock(),
        "source_records": MagicMock(),
        "activities": MagicMock(),
        "stage_history": MagicMock(),
        "audit_events": MagicMock(),
        "admin_users": MagicMock(),
    }
    service = CrmService(repos=CrmRepositories(**repos))
    conn = MagicMock()
    return service, conn, repos


@pytest.mark.unit
def test_company_repository_pipeline_queries() -> None:
    repo = PostgresCompanyRepository()
    overdue_row = {
        "id": COMPANY_ID,
        "name": "Acme",
        "next_action": "Follow up",
        "next_action_due_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "pipeline_stage": "qualified",
    }
    conn = _mock_conn([overdue_row])

    results = repo.list_overdue_actions(
        conn,
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
        limit=10,
    )
    assert len(results) == 1
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "next_action_due_at <" in sql
    assert "pipeline_stage NOT IN ('won', 'lost')" in sql

    upcoming_conn = _mock_conn([overdue_row])
    repo.list_upcoming_actions(
        upcoming_conn,
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
        until=datetime(2026, 7, 8, tzinfo=timezone.utc),
        limit=10,
    )
    upcoming_sql = str(
        upcoming_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    )
    assert "next_action_due_at >=" in upcoming_sql
    assert "next_action_due_at <=" in upcoming_sql


@pytest.mark.unit
def test_stage_history_repository_create_and_list() -> None:
    repo = PostgresStageHistoryRepository()
    row = {
        "id": HISTORY_ID,
        "company_id": COMPANY_ID,
        "from_stage": "researching",
        "to_stage": "qualified",
        "changed_by": "operator",
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        company_id=COMPANY_ID,
        from_stage="researching",
        to_stage="qualified",
        changed_by="operator",
    )
    assert created["to_stage"] == "qualified"

    list_conn = _mock_conn([row])
    history = repo.list_for_company(list_conn, COMPANY_ID)
    assert len(history) == 1
    list_sql = str(list_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "company_stage_history" in list_sql
    assert "ORDER BY changed_at DESC" in list_sql


@pytest.mark.unit
def test_audit_event_repository_create_and_list() -> None:
    repo = PostgresAuditEventRepository()
    row = {
        "id": AUDIT_ID,
        "entity_type": "company",
        "entity_id": COMPANY_ID,
        "action": "stage_change",
        "actor": "operator",
    }
    conn = _mock_conn(row)

    created = repo.create(
        conn,
        entity_type="company",
        entity_id=COMPANY_ID,
        action="stage_change",
        actor="operator",
        metadata={"from_stage": "researching", "to_stage": "qualified"},
    )
    assert created["action"] == "stage_change"

    list_conn = _mock_conn([row])
    events = repo.list_for_entity(
        list_conn,
        entity_type="company",
        entity_id=COMPANY_ID,
    )
    assert len(events) == 1
    list_sql = str(list_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "crm_audit_events" in list_sql


@pytest.mark.unit
def test_transition_company_stage_writes_history_activity_and_audit() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "researching",
    }
    repos["companies"].update.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
    }
    repos["stage_history"].create.return_value = {
        "id": HISTORY_ID,
        "from_stage": "researching",
        "to_stage": "qualified",
    }

    result = service.transition_company_stage(
        conn,
        company_id=COMPANY_ID,
        to_stage="qualified",
        actor="operator",
    )

    assert result["company"]["pipeline_stage"] == "qualified"
    repos["stage_history"].create.assert_called_once()
    repos["activities"].create.assert_called_once()
    repos["audit_events"].create.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_transition_to_lost_requires_reason() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
    }

    with pytest.raises(ReasonRequiredError):
        service.transition_company_stage(
            conn,
            company_id=COMPANY_ID,
            to_stage="lost",
            actor="operator",
        )

    conn.commit.assert_not_called()


@pytest.mark.unit
def test_skip_transition_requires_confirm() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "researching",
    }

    with pytest.raises(ConfirmRequiredError):
        service.transition_company_stage(
            conn,
            company_id=COMPANY_ID,
            to_stage="contacted",
            actor="operator",
        )

    conn.commit.assert_not_called()


@pytest.mark.unit
def test_update_company_next_action_writes_audit_event() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = {
        "id": COMPANY_ID,
        "next_action": "Call",
        "next_action_due_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "owner": "operator",
        "expected_value": 1000,
    }
    repos["companies"].update.return_value = {
        "id": COMPANY_ID,
        "next_action": "Email",
        "next_action_due_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
        "owner": "operator",
        "expected_value": 1500,
    }

    updated = service.update_company_next_action(
        conn,
        company_id=COMPANY_ID,
        actor="operator",
        next_action="Email",
        due_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        expected_value=1500,
    )

    assert updated["next_action"] == "Email"
    repos["audit_events"].create.assert_called_once()
    audit_metadata = repos["audit_events"].create.call_args.kwargs["metadata"]
    assert audit_metadata["before"]["next_action"] == "Call"
    assert audit_metadata["after"]["next_action"] == "Email"


@pytest.mark.unit
def test_list_overdue_and_upcoming_actions_delegate_to_repository() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].list_overdue_actions.return_value = [{"id": COMPANY_ID}]
    repos["companies"].list_upcoming_actions.return_value = [{"id": COMPANY_ID}]

    as_of = datetime(2026, 7, 14, tzinfo=timezone.utc)
    overdue = service.list_overdue_actions(conn, as_of=as_of)
    upcoming = service.list_upcoming_actions(conn, as_of=as_of, within_days=3)

    assert len(overdue) == 1
    assert len(upcoming) == 1
    repos["companies"].list_upcoming_actions.assert_called_once()
    until = repos["companies"].list_upcoming_actions.call_args.kwargs["until"]
    assert until == as_of + timedelta(days=3)


@pytest.mark.unit
def test_record_activity_with_actor_writes_audit_event() -> None:
    service, conn, repos = _service_with_mocks()
    repos["activities"].create.return_value = {
        "id": UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        "activity_type": "outreach",
        "summary": "Sent intro email",
    }

    activity = service.record_activity_for_company(
        conn,
        company_id=COMPANY_ID,
        activity_type="outreach",
        summary="Sent intro email",
        actor="operator",
    )

    assert activity["activity_type"] == "outreach"
    repos["audit_events"].create.assert_called_once()


@pytest.mark.unit
def test_get_company_pipeline_detail_returns_none_for_missing_company() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = None

    assert service.get_company_pipeline_detail(conn, COMPANY_ID) is None


@pytest.mark.unit
def test_get_company_pipeline_detail_assembles_related_records() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "pipeline_stage": "qualified"}
    repos["stage_history"].list_for_company.return_value = [{"to_stage": "qualified"}]
    repos["activities"].list_for_company.return_value = [{"summary": "Call"}]
    repos["audit_events"].list_for_entity.return_value = [{"action": "stage_change"}]

    detail = service.get_company_pipeline_detail(conn, COMPANY_ID)

    assert detail is not None
    assert detail["company"]["pipeline_stage"] == "qualified"
    assert len(detail["stage_history"]) == 1
    assert len(detail["activities"]) == 1
    assert len(detail["audit_events"]) == 1


@pytest.mark.unit
def test_list_companies_by_stage_validates_stage_name() -> None:
    from app.pipeline import InvalidStageError

    service, conn, _ = _service_with_mocks()

    with pytest.raises(InvalidStageError):
        service.list_companies_by_stage(conn, pipeline_stage="bogus")


@pytest.mark.unit
def test_update_company_next_action_raises_for_missing_company() -> None:
    from app.pipeline import InvalidStageError

    service, conn, repos = _service_with_mocks()
    repos["companies"].get_by_id.return_value = None

    with pytest.raises(InvalidStageError):
        service.update_company_next_action(
            conn,
            company_id=COMPANY_ID,
            actor="operator",
            next_action="Call",
        )


@pytest.mark.unit
def test_company_repository_update_pipeline_fields() -> None:
    repo = PostgresCompanyRepository()
    updated = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Follow up",
        "owner": "operator",
        "expected_value": 2500,
    }
    conn = _mock_conn(updated)

    row = repo.update(
        conn,
        COMPANY_ID,
        pipeline_stage="qualified",
        next_action="Follow up",
        owner="operator",
        expected_value=2500,
        clear_stage_reason=True,
    )
    assert row is not None
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "pipeline_stage" in sql
    assert "next_action" in sql
    assert "stage_reason = NULL" in sql


@pytest.mark.unit
def test_company_repository_list_by_pipeline_stage() -> None:
    repo = PostgresCompanyRepository()
    row = {"id": COMPANY_ID, "pipeline_stage": "qualified"}
    conn = _mock_conn([row])

    results = repo.list_by_pipeline_stage(conn, pipeline_stage="qualified", limit=5)
    assert len(results) == 1
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "pipeline_stage = %s" in sql
