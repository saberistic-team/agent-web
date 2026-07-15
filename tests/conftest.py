"""Shared pytest fixtures for admin authentication tests."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "prod-limiter-secret-32chars-minimum!"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure configured admin-auth tests satisfy startup limiter secret validation."""
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET"):
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
