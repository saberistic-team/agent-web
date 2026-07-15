"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum-x"


@pytest.fixture(autouse=True)
def admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a strong limiter secret for every test unless explicitly cleared."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
