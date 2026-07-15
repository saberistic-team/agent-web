"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure login limiter secret validation passes unless a test clears it."""
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_SECRET",
        os.environ.get("ADMIN_LOGIN_LIMITER_SECRET") or TEST_LIMITER_SECRET,
    )
