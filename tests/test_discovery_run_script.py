"""Tests for scripts/discovery_run.py cron entrypoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts import discovery_run


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_script_exits_when_scheduler_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCOVERY_SCHEDULER_ENABLED", raising=False)
    assert discovery_run.main() == 0


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_script_exits_when_not_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCOVERY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("APP_ENV", "production")
    mock_service = MagicMock()
    mock_service.trigger_scheduled_run_if_due.return_value = None
    with (
        patch("scripts.discovery_run.get_settings") as get_settings,
        patch("scripts.discovery_run.db.db_connection") as db_conn,
        patch("scripts.discovery_run.get_discovery_run_service", return_value=mock_service),
    ):
        get_settings.return_value.discovery_schedule_active = True
        get_settings.return_value.database_url = "postgresql://test/db"
        db_conn.return_value.__enter__.return_value = MagicMock()
        assert discovery_run.main() == 0


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_script_returns_error_on_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCOVERY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("APP_ENV", "production")
    mock_service = MagicMock()
    mock_result = MagicMock()
    mock_result.status = "failed"
    mock_result.run_id = "run-id"
    mock_result.lock_acquired = True
    mock_service.trigger_scheduled_run_if_due.return_value = mock_result
    with (
        patch("scripts.discovery_run.get_settings") as get_settings,
        patch("scripts.discovery_run.db.db_connection") as db_conn,
        patch("scripts.discovery_run.get_discovery_run_service", return_value=mock_service),
    ):
        get_settings.return_value.discovery_schedule_active = True
        get_settings.return_value.database_url = "postgresql://test/db"
        db_conn.return_value.__enter__.return_value = MagicMock()
        assert discovery_run.main() == 1


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_run_script_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCOVERY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("scripts.discovery_run.get_settings") as get_settings:
        get_settings.return_value.discovery_schedule_active = True
        get_settings.return_value.database_url = ""
        assert discovery_run.main() == 1
