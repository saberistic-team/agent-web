"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"


@pytest.fixture(autouse=True)
def _admin_login_limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure admin auth tests have keyed limiter secret material configured."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
