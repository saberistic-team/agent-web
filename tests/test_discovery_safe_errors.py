"""Tests for discovery safe error serialization."""

from __future__ import annotations

import pytest

from app.discovery.safe_errors import safe_discovery_errors, safe_error_message
from app.discovery.types import DiscoveryError


@pytest.mark.unit
@pytest.mark.integration
def test_safe_error_message_redacts_secrets() -> None:
    raw = "api_key=supersecret token=abc123 bearer deadbeef"
    safe = safe_error_message(raw)
    assert "supersecret" not in safe
    assert "[redacted]" in safe


@pytest.mark.unit
@pytest.mark.integration
def test_safe_discovery_errors_truncates_messages() -> None:
    errors = [
        DiscoveryError(code="adapter_failure", message="x" * 600),
    ]
    serialized = safe_discovery_errors(errors)
    assert len(serialized[0]["message"]) <= 500
