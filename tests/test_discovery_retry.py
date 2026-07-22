"""Unit tests for discovery retry backoff."""

from __future__ import annotations

import pytest

from app.discovery.retry import retry_delay_seconds, run_with_retries


@pytest.mark.unit
def test_retry_delay_exponential_backoff() -> None:
    assert retry_delay_seconds(0, base_seconds=1.0, cap_seconds=30.0) == 1.0
    assert retry_delay_seconds(1, base_seconds=1.0, cap_seconds=30.0) == 2.0
    assert retry_delay_seconds(10, base_seconds=1.0, cap_seconds=30.0) == 30.0


@pytest.mark.unit
def test_retry_delay_honors_retry_after() -> None:
    assert retry_delay_seconds(0, base_seconds=1.0, cap_seconds=30.0, retry_after="12") == 12.0
    assert retry_delay_seconds(0, base_seconds=1.0, cap_seconds=30.0, retry_after="120") == 30.0


@pytest.mark.unit
def test_run_with_retries_succeeds_after_transient_failure() -> None:
    attempts = {"count": 0}

    def operation() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = run_with_retries(
        operation,
        max_attempts=5,
        base_seconds=0.0,
        cap_seconds=0.0,
        sleep=lambda _delay: None,
    )
    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.unit
def test_run_with_retries_stops_on_non_retryable() -> None:
    attempts = {"count": 0}

    def operation() -> None:
        attempts["count"] += 1
        raise ValueError("fatal")

    with pytest.raises(ValueError, match="fatal"):
        run_with_retries(
            operation,
            max_attempts=5,
            base_seconds=0.0,
            cap_seconds=0.0,
            sleep=lambda _delay: None,
            is_retryable=lambda exc: isinstance(exc, RuntimeError),
        )
    assert attempts["count"] == 1
