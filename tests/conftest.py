"""Shared pytest configuration for the test suite."""

from __future__ import annotations

import os

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"


def pytest_configure(config) -> None:
    """Ensure admin limiter secrets exist before app modules import TestClient."""
    os.environ.setdefault("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
