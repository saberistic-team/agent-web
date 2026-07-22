"""Audit coverage for research evidence and pipeline activity mutations (#334)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service
from app.acquisition_pipeline import PipelineActivityCreate
from app.actor_context import ActorContext
from app.admin_pages import render_admin_audit_page
from app.crm_service import CrmRepositories, CrmService
from app.main import app

client = TestClient(app, follow_redirects=False)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = ActorContext(actor="operator", correlation_id="corr-research-audit")
CSRF_TOKEN = "csrf-research-audit-token"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"

SECRET_BODY = "TOP_SECRET_RESEARCH_BODY_334"
SECRET_SUMMARY = "CONFIDENTIAL_ACTIVITY_SUMMARY_334"
SECRET_URL = "https://reports.example.com/2025?token=sk_live_secret_334"
SECRET_VALUE = "ceo@secret.example"
SECRET_METADATA = {"api_key": "sk_test_334", "note": "private"}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", ACTOR.actor)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.admin_username = ACTOR.actor
    session.csrf_token_hash = "hash"
    return session


@pytest.mark.unit
def test_company_research_create_writes_one_audit_event() -> None:
    research_repo = MagicMock()
    record_id = uuid4()
    research_repo.create.return_value = {
        "id": record_id,
        "record_type": "hypothesis",
        "company_id": COMPANY_ID,
        "created_at": datetime.now(timezone.utc),
    }
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=research_repo,
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch(
        "app.crm_service.audit_service.record_research_record_create",
        wraps=audit_service.record_research_record_create,
    ) as audit:
        with patch("app.crm_service.audit_service.get_repositories") as get_repos:
            audit_repo = MagicMock()
            audit_repo.append.return_value = {"id": "evt-1"}
            get_repos.return_value.audit_events = audit_repo
            service.attach_research_record(
                conn,
                actor_context=ACTOR,
                record_type="hypothesis",
                company_id=COMPANY_ID,
                body="Evaluating vendors",
            )
    audit.assert_called_once()
    payload = audit_repo.append.call_args.kwargs
    assert payload["action"] == audit_service.ACTION_RESEARCH_RECORD_CREATE
    assert payload["actor"] == ACTOR.actor
    assert payload["correlation_id"] == ACTOR.correlation_id
    assert payload["entity_id"] == str(record_id)
    assert payload["summary_after"]["company_id"] == str(COMPANY_ID)
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_contact_research_create_writes_one_audit_event() -> None:
    research_repo = MagicMock()
    record_id = uuid4()
    research_repo.create.return_value = {"id": record_id, "record_type": "verified_fact"}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=research_repo,
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.return_value = {"id": "evt-2"}
        get_repos.return_value.audit_events = audit_repo
        service.attach_research_record(
            conn,
            actor_context=ACTOR,
            record_type="verified_fact",
            company_id=COMPANY_ID,
            contact_id=CONTACT_ID,
            body="Met at conference",
        )
    payload = audit_repo.append.call_args.kwargs
    assert payload["action"] == audit_service.ACTION_RESEARCH_RECORD_CREATE
    assert payload["summary_after"]["contact_id"] == str(CONTACT_ID)


@pytest.mark.unit
def test_pipeline_activity_create_writes_one_audit_event() -> None:
    activity_repo = MagicMock()
    activity_id = uuid4()
    created_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    activity_repo.create.return_value = {
        "id": activity_id,
        "activity_type": "outreach",
        "summary": "Called CEO",
        "created_at": created_at,
    }
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
    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.return_value = {"id": "evt-3"}
        get_repos.return_value.audit_events = audit_repo
        service.record_pipeline_activity(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            activity=PipelineActivityCreate(activity_type="outreach", summary="Called CEO"),
        )
    payload = audit_repo.append.call_args.kwargs
    assert payload["action"] == audit_service.ACTION_PIPELINE_ACTIVITY_CREATE
    assert payload["entity_id"] == str(activity_id)
    assert payload["summary_after"]["activity_type"] == "outreach"
    assert payload["summary_after"]["company_id"] == str(COMPANY_ID)
    assert "Called CEO" not in json.dumps(payload["summary_after"])


@pytest.mark.unit
def test_audit_json_excludes_research_and_activity_free_form_content() -> None:
    research_repo = MagicMock()
    research_repo.create.return_value = {"id": uuid4()}
    activity_repo = MagicMock()
    activity_repo.create.return_value = {
        "id": uuid4(),
        "created_at": datetime.now(timezone.utc),
    }
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=activity_repo,
            research_records=research_repo,
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    captured: list[dict[str, Any]] = []

    def capture_append(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt"}

    with patch("app.crm_service.audit_service.get_repositories") as get_repos:
        audit_repo = MagicMock()
        audit_repo.append.side_effect = capture_append
        get_repos.return_value.audit_events = audit_repo
        service.attach_research_record(
            conn,
            actor_context=ACTOR,
            record_type="verified_fact",
            company_id=COMPANY_ID,
            body=SECRET_BODY,
            source_url=SECRET_URL,
            observed_value=SECRET_VALUE,
            metadata=SECRET_METADATA,
        )
        service.record_pipeline_activity(
            conn,
            actor_context=ACTOR,
            company_id=COMPANY_ID,
            activity=PipelineActivityCreate(
                activity_type="note",
                summary=SECRET_SUMMARY,
                metadata=SECRET_METADATA,
            ),
        )

    assert len(captured) == 2
    combined = json.dumps(captured)
    for secret in (
        SECRET_BODY,
        SECRET_SUMMARY,
        SECRET_URL,
        SECRET_VALUE,
        "sk_test_334",
        "sk_live_secret_334",
        "ceo@secret.example",
    ):
        assert secret not in combined
    research_summary = captured[0]["summary_after"]
    assert research_summary["has_source_url"] is True
    assert research_summary["has_observed_value"] is True
    assert "source_url" not in research_summary
    assert "body" not in research_summary


@pytest.mark.unit
def test_research_audit_failure_rolls_back_without_success_event() -> None:
    research_repo = MagicMock()
    research_repo.create.return_value = {"id": uuid4()}
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=research_repo,
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch(
        "app.crm_service.audit_service.record_research_record_create",
        side_effect=RuntimeError("audit sink unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit sink unavailable"):
            service.attach_research_record(
                conn,
                actor_context=ACTOR,
                record_type="hypothesis",
                company_id=COMPANY_ID,
                body="Should roll back",
            )
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.unit
def test_research_repository_failure_writes_no_audit_event() -> None:
    research_repo = MagicMock()
    research_repo.create.side_effect = ValueError("invalid record")
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=research_repo,
            admin_users=MagicMock(),
            pipeline=MagicMock(),
            import_batches=MagicMock(),
            icp_scoring=MagicMock(),
            qualification=MagicMock(),
        )
    )
    conn = MagicMock()
    with patch("app.crm_service.audit_service.record_research_record_create") as audit:
        with pytest.raises(ValueError, match="invalid record"):
            service.attach_research_record(
                conn,
                actor_context=ACTOR,
                record_type="hypothesis",
                company_id=COMPANY_ID,
                body="Never commits",
            )
    audit.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_anonymous_and_invalid_csrf_research_requests_do_not_mutate() -> None:
    unauthenticated = client.post(
        f"/admin/companies/{COMPANY_ID}/research",
        data={"csrf_token": CSRF_TOKEN, "record_type": "hypothesis", "body": "Nope"},
    )
    assert unauthenticated.status_code == 303
    assert "/admin/login" in unauthenticated.headers["location"]

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._crm") as crm:
            bad_csrf = client.post(
                f"/admin/companies/{COMPANY_ID}/research",
                data={"csrf_token": "wrong", "record_type": "hypothesis", "body": "Nope"},
            )
            assert bad_csrf.status_code == 400
            crm.attach_research_record.assert_not_called()


@pytest.mark.unit
def test_anonymous_and_invalid_csrf_pipeline_activity_do_not_mutate() -> None:
    unauthenticated = client.post(
        f"/admin/pipeline/{COMPANY_ID}/activities",
        data={"csrf_token": CSRF_TOKEN, "activity_type": "note", "summary": "Nope"},
    )
    assert unauthenticated.status_code == 303
    assert "/admin/login" in unauthenticated.headers["location"]

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_pipeline_routes._crm") as crm:
            bad_csrf = client.post(
                f"/admin/pipeline/{COMPANY_ID}/activities",
                data={"csrf_token": "wrong", "activity_type": "note", "summary": "Nope"},
            )
            assert bad_csrf.status_code == 400
            crm.record_pipeline_activity.assert_not_called()


@pytest.mark.unit
def test_audit_ui_renders_new_event_types_with_bounded_summaries() -> None:
    events = [
        {
            "created_at": datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_RESEARCH_RECORD_CREATE,
            "entity_type": "research_record",
            "entity_id": "rec-1",
            "correlation_id": ACTOR.correlation_id,
            "summary_before": None,
            "summary_after": {
                "record_type": "hypothesis",
                "company_id": str(COMPANY_ID),
                "has_source_name": True,
                "has_source_url": True,
            },
        },
        {
            "created_at": datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc),
            "actor": ACTOR.actor,
            "action": audit_service.ACTION_PIPELINE_ACTIVITY_CREATE,
            "entity_type": "pipeline_activity",
            "entity_id": "act-1",
            "correlation_id": ACTOR.correlation_id,
            "summary_before": None,
            "summary_after": {
                "activity_type": "outreach",
                "company_id": str(COMPANY_ID),
                "created_at": "2026-07-14T13:00:00+00:00",
            },
        },
    ]
    html_out = render_admin_audit_page(
        admin_username=ACTOR.actor,
        events=events,
        page=1,
        per_page=50,
        total=2,
    )
    assert "Research evidence added" in html_out
    assert "research_record.create" in html_out
    assert "Pipeline activity logged" in html_out
    assert "pipeline_activity.create" in html_out
    assert "type=hypothesis" in html_out
    assert "type=outreach" in html_out
    assert "<script>" not in html_out
