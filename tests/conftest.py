"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LOGIN_LIMITER_SECRET = "test-login-limiter-secret-32chars-min!!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure limiter HMAC secret is present unless a test overrides it."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LOGIN_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
