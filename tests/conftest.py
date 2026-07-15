"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


@pytest.fixture(autouse=True)
def default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure admin auth tests have a strong limiter secret unless explicitly cleared."""
    if not (os.environ.get("ADMIN_LOGIN_LIMITER_SECRET") or "").strip():
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
