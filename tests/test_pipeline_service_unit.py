"""Unit tests for pipeline CRM service methods."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.acquisition_pipeline import PipelineNextActionUpdate, PipelineStageChange, PipelineTransitionError
from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR = ActorContext(actor="operator", correlation_id="corr-1")


@pytest.mark.unit
@pytest.mark.integration
def test_transition_pipeline_stage_persists_history_activity_and_audit() -> None:
    pipeline_repo = MagicMock()
    activity_repo = MagicMock()
    company = {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
    }
    updated = {**company, "pipeline_stage": "qualified"}
    pipeline_repo.get_company_pipeline.return_value = company
    pipeline_repo.update_pipeline_fields.return_value = updated
    pipeline_repo.record_stage_history.return_value = {"id": "hist-1"}
    activity_repo.create.return_value = {"id": "act-1"}

    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()

    with patch("app.crm_service.audit_service.record_pipeline_update") as audit:
        result = service.transition_pipeline_stage(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            change=PipelineStageChange(to_stage="qualified"),
        )

    assert result["company"]["pipeline_stage"] == "qualified"
    pipeline_repo.record_stage_history.assert_called_once()
    activity_repo.create.assert_called_once()
    audit.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_transition_pipeline_stage_rejects_invalid_transition() -> None:
    pipeline_repo = MagicMock()
    pipeline_repo.get_company_pipeline.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "researching",
    }
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with pytest.raises(PipelineTransitionError):
        service.transition_pipeline_stage(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            change=PipelineStageChange(to_stage="contacted"),
        )
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_list_pipeline_overdue_actions_delegates_to_repo() -> None:
    pipeline_repo = MagicMock()
    pipeline_repo.list_overdue_next_actions.return_value = [{"name": "Acme"}]
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    ref = datetime(2026, 7, 14, tzinfo=timezone.utc)
    rows = service.list_pipeline_overdue_actions(MagicMock(), reference=ref, limit=3)
    assert rows[0]["name"] == "Acme"
    pipeline_repo.list_overdue_next_actions.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_update_pipeline_next_action_audits_change() -> None:
    pipeline_repo = MagicMock()
    company = {"id": COMPANY_ID, "pipeline_stage": "qualified", "next_action": "Old"}
    updated = {**company, "next_action": "New"}
    pipeline_repo.get_company_pipeline.return_value = company
    pipeline_repo.update_pipeline_fields.return_value = updated
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch("app.crm_service.audit_service.record_pipeline_update") as audit:
        result = service.update_pipeline_next_action(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            update=PipelineNextActionUpdate(next_action="New"),
        )
    assert result["company"]["next_action"] == "New"
    audit.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_record_pipeline_activity_commits() -> None:
    activity_repo = MagicMock()
    activity_repo.create.return_value = {"id": "act-1", "summary": "Called CEO"}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    from app.acquisition_pipeline import PipelineActivityCreate
    from app.actor_context import ActorContext

    actor = ActorContext(actor="admin", correlation_id="test")
    with patch("app.crm_service.audit_service.record_pipeline_activity_create"):
        row = service.record_pipeline_activity(
            conn,
            actor_context=actor,
            company_id=COMPANY_ID,
            activity=PipelineActivityCreate(activity_type="outreach", summary="Called CEO"),
        )
    assert row["summary"] == "Called CEO"
    conn.commit.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_complete_queue_item_clears_next_action_and_audits() -> None:
    pipeline_repo = MagicMock()
    activity_repo = MagicMock()
    company = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Follow up",
        "next_action_due_at": datetime(2026, 7, 16, tzinfo=timezone.utc),
    }
    updated = {**company, "next_action": None, "next_action_due_at": None}
    pipeline_repo.get_company_pipeline.return_value = company
    pipeline_repo.update_pipeline_fields.return_value = updated
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch("app.crm_service.audit_service.record_pipeline_update") as audit:
        result = service.complete_queue_item(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            item_key="overdue:1",
            item_category="overdue_action",
        )
    assert result["status"] == "completed"
    activity_repo.create.assert_called_once()
    audit.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_snooze_queue_item_extends_due_date() -> None:
    pipeline_repo = MagicMock()
    activity_repo = MagicMock()
    due = datetime(2026, 7, 16, tzinfo=timezone.utc)
    company = {
        "id": COMPANY_ID,
        "pipeline_stage": "qualified",
        "next_action": "Follow up",
        "next_action_due_at": due,
    }
    pipeline_repo.get_company_pipeline.return_value = company
    pipeline_repo.update_pipeline_fields.return_value = company
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    result = service.snooze_queue_item(
        conn,
        actor_context=ACTOR,
        company_id=COMPANY_ID,
        item_key="overdue:1",
        item_category="overdue_action",
        snooze_days=7,
    )
    assert result["next_action_due_at"] == due + timedelta(days=7)
    activity_repo.create.assert_called_once()


@pytest.mark.unit
def test_reschedule_queue_item_updates_due_at() -> None:
    pipeline_repo = MagicMock()
    activity_repo = MagicMock()
    pipeline_repo.get_company_pipeline.return_value = {"id": COMPANY_ID}
    pipeline_repo.update_pipeline_fields.return_value = {"id": COMPANY_ID}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    new_due = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    result = service.reschedule_queue_item(
        conn,
        actor_context=ACTOR,
        company_id=COMPANY_ID,
        item_key="due_today:1",
        item_category="due_today_action",
        next_action_due_at=new_due,
    )
    assert result["next_action_due_at"] == new_due
    activity_repo.create.assert_called_once()


@pytest.mark.unit
def test_replace_queue_item_records_activity() -> None:
    pipeline_repo = MagicMock()
    activity_repo = MagicMock()
    pipeline_repo.get_company_pipeline.return_value = {"id": COMPANY_ID}
    pipeline_repo.update_pipeline_fields.return_value = {"id": COMPANY_ID}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=MagicMock(),
            admin_users=MagicMock(),
            pipeline=pipeline_repo,
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    update = PipelineNextActionUpdate(
        next_action="Send deck",
        next_action_due_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    conn = MagicMock()
    result = service.replace_queue_item(
        conn,
        actor_context=ACTOR,
        company_id=COMPANY_ID,
        item_key="overdue:1",
        item_category="overdue_action",
        update=update,
    )
    assert result["status"] == "replaced"
    activity_repo.create.assert_called_once()


@pytest.mark.unit
def test_request_export_records_audit() -> None:
    service = CrmService(
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
    conn = MagicMock()
    with patch("app.crm_service.audit_service.record_export_request") as audit:
        result = service.request_export(
            conn,
            actor_context=ACTOR,
            export_type="acquisition_queue_csv",
            filters={"source": "action_queue"},
        )
    assert result["export_type"] == "acquisition_queue_csv"
    audit.assert_called_once()
    conn.commit.assert_called_once()
