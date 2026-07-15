"""Unit tests for verify_admin_proxy_trust deployment script."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts import verify_admin_proxy_trust


@pytest.mark.unit
def test_verify_admin_proxy_trust_passes_configured() -> None:
    payload = {"status": "ok", "admin_proxy_trust": "configured"}
    with patch(
        "scripts.verify_admin_proxy_trust.fetch_health",
        return_value=payload,
    ):
        assert verify_admin_proxy_trust.main(["--require-configured"]) == 0


@pytest.mark.unit
def test_verify_admin_proxy_trust_fails_when_not_configured() -> None:
    payload = {"status": "ok", "admin_proxy_trust": "disabled"}
    with patch(
        "scripts.verify_admin_proxy_trust.fetch_health",
        return_value=payload,
    ):
        assert verify_admin_proxy_trust.main(["--require-configured"]) == 1
