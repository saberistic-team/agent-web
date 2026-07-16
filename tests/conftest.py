"""Shared pytest fixtures for admin security configuration."""

from __future__ import annotations

import pytest

TEST_LOGIN_LIMITER_SECRET = "test-login-limiter-secret-32chars-min!!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests satisfy startup limiter-secret validation when admin auth is enabled."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LOGIN_LIMITER_SECRET)
