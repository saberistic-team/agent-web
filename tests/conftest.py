"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min!!"


@pytest.fixture(autouse=True)
def admin_login_limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a strong limiter secret unless a case overrides it."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
