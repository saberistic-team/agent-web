"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import pytest

TEST_LIMITER_SECRET = "test-login-limiter-key-32bytes-minimum!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure admin auth tests have strong limiter key material unless overridden."""
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
