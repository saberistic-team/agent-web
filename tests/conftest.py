"""Shared pytest fixtures for admin authentication tests."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"
TEST_LIMITER_SECRET_PREVIOUS = "test-limiter-secret-prev-32chars-min!!"


@pytest.fixture(autouse=True)
def _admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a strong limiter secret unless a test overrides it."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
