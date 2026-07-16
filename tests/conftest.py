"""Shared pytest defaults for admin security env vars."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min"


def enable_admin_preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://127.0.0.1:8765",
    bind_host: str = "127.0.0.1",
    app_env: str = "development",
) -> None:
    """Set the env contract required for a safe admin preview bypass."""
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SERVER_BIND_HOST", bind_host)
    monkeypatch.setenv("BASE_URL", base_url)


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
