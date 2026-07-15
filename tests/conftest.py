"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure admin auth tests have a strong limiter secret configured."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
