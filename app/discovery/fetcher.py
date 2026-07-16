"""Bounded HTTP fetcher for permission-aware discovery sources."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.discovery.rate_limit import RateLimiter

DEFAULT_MAX_BYTES = 512_000
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RATE_LIMIT_RPM = 6

DISCOVERY_USER_AGENT = (
    "SaberisticDiscoveryBot/1.0 (+https://saberistic.com/; lead-discovery)"
)

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "text/xml",
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/json",
        "application/xhtml+xml",
    }
)

_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal"})


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool
    robots_allowed: bool | None


@dataclass(frozen=True)
class FetchPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DISCOVERY_USER_AGENT
    rate_limit_requests_per_minute: int = DEFAULT_RATE_LIMIT_RPM


class FetchError(Exception):
    """Raised when a fetch violates discovery safety policy."""


@dataclass
class FetchCacheEntry:
    etag: str | None
    last_modified: str | None
    body: bytes
    content_type: str
    status_code: int


class FetchCache:
    """In-memory conditional-request cache for discovery fetches."""

    def __init__(self) -> None:
        self._entries: dict[str, FetchCacheEntry] = {}

    def get(self, url: str) -> FetchCacheEntry | None:
        return self._entries.get(url)

    def put(self, url: str, entry: FetchCacheEntry) -> None:
        self._entries[url] = entry


def _hostname_blocked(hostname: str) -> bool:
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTS:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return False


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise FetchError("URL must use http or https")
    if not parsed.hostname:
        raise FetchError("URL must include a hostname")
    if _hostname_blocked(parsed.hostname):
        raise FetchError("hostname is blocked")
    if parsed.username or parsed.password:
        raise FetchError("URL must not include credentials")
    try:
        infos = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except socket.gaierror as exc:
        raise FetchError("hostname did not resolve") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _ip_blocked(ip):
            raise FetchError("hostname resolves to a private or local address")
    return url.strip()


def normalize_content_type(raw: str) -> str:
    return raw.split(";", 1)[0].strip().lower()


def enforce_content_type(content_type: str) -> None:
    if normalize_content_type(content_type) not in ALLOWED_CONTENT_TYPES:
        raise FetchError(f"content type not allowed: {content_type}")


def enforce_size(body: bytes, *, max_bytes: int) -> None:
    if len(body) > max_bytes:
        raise FetchError(f"response exceeds {max_bytes} bytes")


class HttpFetcher:
    """HTTP retrieval with timeouts, size limits, caching, and rate limits."""

    def __init__(
        self,
        *,
        policy: FetchPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: FetchCache | None = None,
        fixture_loader: Callable[[str], bytes] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.policy = policy or FetchPolicy()
        self.rate_limiter = rate_limiter or RateLimiter(
            requests_per_minute=self.policy.rate_limit_requests_per_minute
        )
        self.cache = cache
        self.fixture_loader = fixture_loader
        self._client = client

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        skip_dns_validation: bool = False,
    ) -> FetchResult:
        if self.fixture_loader is not None:
            return self._fetch_fixture(
                url,
                skip_dns_validation=True,
            )
        return self._fetch_live(
            url,
            etag=etag,
            last_modified=last_modified,
            skip_dns_validation=skip_dns_validation or self._client is not None,
        )

    def _fetch_fixture(
        self,
        url: str,
        *,
        skip_dns_validation: bool,
    ) -> FetchResult:
        if skip_dns_validation:
            parsed = urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise FetchError("URL must use http or https with a hostname")
        else:
            validate_public_url(url)
        try:
            body = self.fixture_loader(url)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 — surface fixture failures as fetch errors
            raise FetchError(str(exc)) from exc
        enforce_size(body, max_bytes=self.policy.max_bytes)
        content_type = "text/html"
        if url.endswith(".xml"):
            content_type = "application/xml"
        elif url.endswith(".json"):
            content_type = "application/json"
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type=content_type,
            body=body,
            etag=None,
            last_modified=None,
            not_modified=False,
            robots_allowed=True,
        )

    def _fetch_live(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
        skip_dns_validation: bool,
    ) -> FetchResult:
        if skip_dns_validation:
            parsed = urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise FetchError("URL must use http or https with a hostname")
            hostname = parsed.hostname
        else:
            validated = validate_public_url(url)
            hostname = urlparse(validated).hostname or ""
        self.rate_limiter.wait_if_needed(hostname)

        headers = {"User-Agent": self.policy.user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(
                timeout=self.policy.timeout_seconds,
                follow_redirects=True,
                max_redirects=self.policy.max_redirects,
            )
        try:
            assert client is not None
            response = client.get(url, headers=headers)
        finally:
            if owns_client and client is not None:
                client.close()

        if response.status_code == 304 and self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=304,
                    content_type=cached.content_type,
                    body=cached.body,
                    etag=cached.etag,
                    last_modified=cached.last_modified,
                    not_modified=True,
                    robots_allowed=True,
                )

        content_type = response.headers.get("content-type", "application/octet-stream")
        enforce_content_type(content_type)
        body = response.content
        enforce_size(body, max_bytes=self.policy.max_bytes)

        response_etag = response.headers.get("etag")
        response_last_modified = response.headers.get("last-modified")
        if self.cache is not None and response.status_code == 200:
            self.cache.put(
                url,
                FetchCacheEntry(
                    etag=response_etag,
                    last_modified=response_last_modified,
                    body=body,
                    content_type=normalize_content_type(content_type),
                    status_code=response.status_code,
                ),
            )

        return FetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            content_type=normalize_content_type(content_type),
            body=body,
            etag=response_etag,
            last_modified=response_last_modified,
            not_modified=False,
            robots_allowed=True,
        )
