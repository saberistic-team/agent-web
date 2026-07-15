"""Shared pytest defaults for admin security env vars."""

from __future__ import annotations

import os

_TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"

os.environ.setdefault("ADMIN_LOGIN_LIMITER_SECRET", _TEST_LIMITER_SECRET)
