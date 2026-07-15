"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-login-limiter-secret-32bytes-min!!"


@pytest.fixture(autouse=True)
def _admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET"):
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
