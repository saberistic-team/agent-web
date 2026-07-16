"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"


@pytest.fixture(autouse=True)
def _default_admin_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure keyed limiter identifiers work unless a test overrides the secret."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
