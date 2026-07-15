"""Shared admin test environment defaults."""

from __future__ import annotations

import os

import pytest

TEST_LIMITER_SECRET = "limiter-key-material-32-bytes-min!!"


@pytest.fixture(autouse=True)
def _default_admin_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("ADMIN_LOGIN_LIMITER_SECRET", "").strip():
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
