"""Bounded HTTP fetcher with SSRF and abuse defenses (spike prototype)."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

DEFAULT_MAX_BYTES = 512_000
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
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
    robots_allowed: bool | None


@dataclass(frozen=True)
class FetchPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    respect_robots: bool = True


class FetchError(Exception):
    """Raised when a fetch violates spike safety policy."""


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
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
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


def strip_html_to_text(html: str) -> str:
    """Minimal HTML-to-text sanitizer for spike extraction (not a full XSS policy)."""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_fixture(
    url: str,
    *,
    fixture_loader: Callable[[str], bytes],
    skip_dns_validation: bool = False,
) -> FetchResult:
    """Offline fetch path used by spike tests and reproducible benchmarks."""
    if skip_dns_validation:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FetchError("URL must use http or https with a hostname")
    else:
        validate_public_url(url)
    body = fixture_loader(url)
    enforce_size(body, max_bytes=DEFAULT_MAX_BYTES)
    content_type = "text/html"
    if url.endswith(".md"):
        content_type = "text/markdown"
    elif url.endswith(".json"):
        content_type = "application/json"
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type=content_type,
        body=body,
        robots_allowed=True,
    )
