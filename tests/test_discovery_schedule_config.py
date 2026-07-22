"""Tests for discovery schedule configuration."""

from __future__ import annotations

import pytest

from app.app_environment import AppEnvironment
from app.config import Settings


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
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_schedule_active_requires_production_flag_and_db() -> None:
    assert _settings(discovery_scheduler_enabled=True).discovery_schedule_active is True
    assert _settings(discovery_scheduler_enabled=False).discovery_schedule_active is False
    assert (
        _settings(
            discovery_scheduler_enabled=True,
            app_environment=AppEnvironment.DEVELOPMENT,
        ).discovery_schedule_active
        is False
    )
    assert (
        _settings(discovery_scheduler_enabled=True, database_url="").discovery_schedule_active
        is False
    )


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_enabled_source_ids_parsed_from_env_string() -> None:
    settings = _settings(discovery_enabled_sources="ycombinator, rss-demo ,api")
    assert settings.discovery_enabled_source_ids == ["ycombinator", "rss-demo", "api"]


@pytest.mark.unit
@pytest.mark.integration
def test_default_schedule_interval_is_weekly() -> None:
    settings = _settings()
    assert settings.discovery_schedule_interval_days == 7
