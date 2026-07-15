"""Bounded HTTP fetcher for WorldGraph spike ingestion tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.worldgraph_spike.security import (
    SSRFBlockedError,
    UnsafeContentError,
    enforce_fetch_limits,
    validate_public_http_url,
)

DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/ld+json",
    }
)

FetchTransport = Callable[[str], httpx.Response]


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    body: bytes
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    redirect_chain: list[str] = field(default_factory=list)
    robots_allowed: bool | None = None


@dataclass
class FetchPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    respect_robots: bool = True


class BoundedFetcher:
    """Safe fetcher with SSRF blocking, redirect limits, and size caps."""

    def __init__(
        self,
        *,
        policy: FetchPolicy | None = None,
        transport: FetchTransport | None = None,
        robots_checker: Callable[[str], bool] | None = None,
    ) -> None:
        self.policy = policy or FetchPolicy()
        self._transport = transport
        self._robots_checker = robots_checker

    def fetch(self, url: str) -> FetchResult:
        current = validate_public_http_url(url)
        if self.policy.respect_robots and self._robots_checker is not None:
            if not self._robots_checker(current):
                raise UnsafeContentError("robots.txt disallows fetch")

        redirect_chain: list[str] = [current]
        response = self._request(current)

        while response.is_redirect:
            if len(redirect_chain) > self.policy.max_redirects:
                raise UnsafeContentError("redirect limit exceeded")
            location = response.headers.get("location")
            if not location:
                raise UnsafeContentError("redirect without location header")
            next_url = httpx.URL(current).join(location)
            current = validate_public_http_url(str(next_url))
            redirect_chain.append(current)
            response = self._request(current)

        body = response.content
        content_type = response.headers.get("content-type")
        enforce_fetch_limits(
            content_type=content_type,
            content_length=_parse_content_length(response.headers.get("content-length")),
            body_size=len(body),
            max_bytes=self.policy.max_bytes,
            allowed_content_types=ALLOWED_CONTENT_TYPES,
        )
        return FetchResult(
            url=url,
            final_url=current,
            status_code=response.status_code,
            content_type=content_type,
            body=body,
            redirect_chain=redirect_chain,
            robots_allowed=True if self._robots_checker is None else True,
        )

    def _request(self, url: str) -> httpx.Response:
        if self._transport is not None:
            return self._transport(url)
        with httpx.Client(
            timeout=self.policy.timeout_seconds,
            follow_redirects=False,
        ) as client:
            return client.get(url)


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def fixture_transport(fixtures: dict[str, bytes]) -> FetchTransport:
    """Return canned responses keyed by canonical URL for offline tests."""

    def _transport(url: str) -> httpx.Response:
        key = validate_public_http_url(url)
        if key not in fixtures:
            raise SSRFBlockedError(f"no fixture for url: {key}")
        body = fixtures[key]
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body,
        )

    return _transport
