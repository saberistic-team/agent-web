"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os

import pytest
from argon2 import PasswordHasher

TEST_LIMITER_SECRET = "test-login-limiter-secret-32bytes!!"
TEST_ADMIN_USERNAME = "operator"
TEST_ADMIN_PASSWORD = "correct-horse-battery-staple"
TEST_ADMIN_PASSWORD_HASH = PasswordHasher().hash(TEST_ADMIN_PASSWORD)
TEST_ADMIN_SESSION_SECRET = "test-session-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure limiter HMAC key material is present unless a test overrides it."""
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET"):
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)


@pytest.fixture(autouse=True)
def _default_admin_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline admin auth env for route tests unless a test overrides it."""
    defaults = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_ADMIN_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_ADMIN_PASSWORD_HASH,
        "ADMIN_SESSION_SECRET": TEST_ADMIN_SESSION_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
        "ADMIN_LOGIN_RATE_LIMIT": "5",
        "ADMIN_LOGIN_RATE_WINDOW_SECONDS": "900",
        "ADMIN_LOGIN_LOCKOUT_SECONDS": "900",
    }
    for key, value in defaults.items():
        if not os.environ.get(key):
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
