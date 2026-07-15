"""Shared pytest defaults for admin security configuration."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum-xx"


@pytest.fixture(autouse=True)
def _default_admin_login_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET"):
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
