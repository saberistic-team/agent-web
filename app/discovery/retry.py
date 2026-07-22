"""Bounded exponential backoff for discovery retries."""

from __future__ import annotations

import time
from typing import Callable

RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: float,
    cap_seconds: float,
    retry_after: str | None = None,
) -> float:
    """Exponential backoff capped at ``cap_seconds``; honor Retry-After when present."""
    if retry_after:
        try:
            return min(float(retry_after.strip()), cap_seconds)
        except ValueError:
            pass
    return min(base_seconds * (2**attempt), cap_seconds)


def run_with_retries(
    operation: Callable[[], object],
    *,
    max_attempts: int,
    base_seconds: float,
    cap_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    is_retryable: Callable[[Exception], bool] | None = None,
) -> object:
    """Run ``operation`` with bounded retries for retryable failures."""
    attempts = max(max_attempts, 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 — caller classifies retryable failures
            last_error = exc
            retryable = is_retryable(exc) if is_retryable is not None else True
            if not retryable or attempt >= attempts - 1:
                raise
            delay = retry_delay_seconds(
                attempt,
                base_seconds=base_seconds,
                cap_seconds=cap_seconds,
                retry_after=_retry_after_header(exc),
            )
            sleep(delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError("retry loop exited without result")


def _retry_after_header(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return headers.get("Retry-After")
