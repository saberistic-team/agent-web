"""Tests for DiscoveryRunService entry points."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.app_environment import AppEnvironment
from app.config import Settings
from app.discovery.orchestrator import DiscoveryOrchestrationResult
from app.discovery.service import DiscoveryRunService

RUN_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd01")


def _settings(**overrides: object) -> Settings:
    base = dict(
        database_url="postgresql://test/db",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url="https://saberistic.com",
        analytics_environment="production",
        app_environment=AppEnvironment.PRODUCTION,
        admin_username="admin",
        admin_password_hash="hash",
        admin_session_secret="secret",
        admin_login_limiter_secret="limiter",
        discovery_scheduler_enabled=True,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_run_config_from_settings() -> None:
    settings = _settings(
        discovery_retry_max_attempts=3,
        discovery_retry_base_seconds=2.0,
        discovery_retry_cap_seconds=15.0,
        discovery_schedule_interval_days=14,
    )
    service = DiscoveryRunService()
    config = service.run_config(settings)
    assert config.retry_max_attempts == 3
    assert config.retry_base_seconds == 2.0
    assert config.retry_cap_seconds == 15.0
    assert config.schedule_interval_days == 14


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_list_and_get_run() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.list_page.return_value = ([{"id": RUN_ID}], 1)
    repo.get_by_id.return_value = {"id": RUN_ID, "status": "completed"}
    repo.list_sources_for_run.return_value = [{"source_id": "ycombinator"}]
    service = DiscoveryRunService(repo=repo)

    runs, total = service.list_runs(conn, page=1)
    assert total == 1
    assert runs[0]["id"] == RUN_ID

    state = service.get_run(conn, RUN_ID)
    assert state is not None
    assert state["run"]["status"] == "completed"
    assert state["sources"][0]["source_id"] == "ycombinator"

    repo.get_by_id.return_value = None
    assert service.get_run(conn, UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")) is None


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_trigger_manual_run() -> None:
    conn = MagicMock()
    settings = _settings()
    expected = DiscoveryOrchestrationResult(
        run_id=RUN_ID,
        status="completed",
        lock_acquired=True,
    )
    with patch(
        "app.discovery.service.execute_discovery_run",
        return_value=expected,
    ) as execute:
        service = DiscoveryRunService(repo=MagicMock())
        result = service.trigger_manual_run(
            conn,
            settings,
            actor="operator",
            correlation_id="corr-manual",
        )
    assert result.run_id == RUN_ID
    assert execute.call_args.kwargs["trigger_type"] == "manual"
    assert execute.call_args.kwargs["actor"] == "operator"


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_scheduled_run_inactive_returns_none() -> None:
    conn = MagicMock()
    settings = _settings(discovery_scheduler_enabled=False)
    service = DiscoveryRunService(repo=MagicMock())
    assert service.trigger_scheduled_run_if_due(conn, settings, correlation_id="c") is None


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_scheduled_run_not_due_returns_none() -> None:
    conn = MagicMock()
    settings = _settings()
    repo = MagicMock()
    repo.latest_scheduled_started_at.return_value = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    service = DiscoveryRunService(repo=repo)
    assert service.trigger_scheduled_run_if_due(conn, settings, correlation_id="c") is None


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_service_scheduled_run_executes_when_due() -> None:
    conn = MagicMock()
    settings = _settings()
    repo = MagicMock()
    repo.latest_scheduled_started_at.return_value = None
    expected = DiscoveryOrchestrationResult(
        run_id=RUN_ID,
        status="completed",
        lock_acquired=True,
    )
    with patch(
        "app.discovery.service.execute_discovery_run",
        return_value=expected,
    ) as execute:
        service = DiscoveryRunService(repo=repo)
        result = service.trigger_scheduled_run_if_due(
            conn,
            settings,
            correlation_id="corr-scheduled",
        )
    assert result is not None
    assert result.run_id == RUN_ID
    assert execute.call_args.kwargs["trigger_type"] == "scheduled"
    assert execute.call_args.kwargs["actor"] == "scheduler"
