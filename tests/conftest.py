"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


@pytest.fixture(autouse=True)
def admin_login_limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a strong limiter secret unless a test overrides it."""
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET", "").strip():
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
