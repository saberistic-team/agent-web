"""Conservative rate limiting for discovery HTTP retrieval."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """Token-bucket style limiter keyed by hostname."""

    requests_per_minute: int
    _last_request_at: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")

    @property
    def min_interval_seconds(self) -> float:
        return 60.0 / float(self.requests_per_minute)

    def wait_if_needed(self, host: str) -> None:
        """Block until the next request to host is permitted."""
        now = time.monotonic()
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = now - last
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at[host] = time.monotonic()

    def would_allow(self, host: str, *, now: float | None = None) -> bool:
        """Return True when a request to host would be allowed immediately."""
        reference = now if now is not None else time.monotonic()
        last = self._last_request_at.get(host)
        if last is None:
            return True
        return (reference - last) >= self.min_interval_seconds

    def record_request(self, host: str, *, now: float | None = None) -> None:
        """Record a completed request without blocking."""
        self._last_request_at[host] = now if now is not None else time.monotonic()
