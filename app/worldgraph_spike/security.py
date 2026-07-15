"""Security helpers for WorldGraph spike fetch/extract paths."""

from __future__ import annotations

import html
import ipaddress
import re
from urllib.parse import urlparse

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.azure.com",
    }
)

PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

INJECTION_MARKERS = (
    "ignore previous instructions",
    "system:",
    "assistant:",
    "you are now",
    "disregard all",
    "<|im_start|>",
)

_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_EVENT_HANDLER_RE = re.compile(r"\s+on\w+\s*=", re.IGNORECASE)


class SSRFBlockedError(ValueError):
    """Raised when a URL targets a private or disallowed network."""


class UnsafeContentError(ValueError):
    """Raised when fetched content fails policy checks."""


def validate_public_http_url(url: str) -> str:
    """Reject schemes and hosts that enable SSRF against internal networks."""
    stripped = url.strip()
    parsed = urlparse(stripped)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise SSRFBlockedError("only http(s) URLs are allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SSRFBlockedError("URL must include a host")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise SSRFBlockedError(f"blocked hostname: {host}")
    if host.endswith((".internal", ".local", ".corp")):
        raise SSRFBlockedError(f"blocked internal suffix: {host}")
    _reject_private_host(host)
    if parsed.username or parsed.password:
        raise SSRFBlockedError("URL must not embed credentials")
    return stripped


def _reject_private_host(host: str) -> None:
    if host == "0.0.0.0":
        raise SSRFBlockedError("blocked host 0.0.0.0")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return
    for network in PRIVATE_NETWORKS:
        if addr in network:
            raise SSRFBlockedError(f"private/reserved address: {host}")


def canonicalize_url(url: str) -> str:
    """Normalize URL for duplicate detection (scheme/host/path, drop fragment)."""
    parsed = urlparse(validate_public_http_url(url))
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{path}"


def sanitize_html_for_storage(text: str) -> str:
    """Strip script tags and event handlers; escape remaining HTML."""
    without_scripts = _SCRIPT_TAG_RE.sub("", text)
    without_handlers = _EVENT_HANDLER_RE.sub(" blocked=", without_scripts)
    return html.escape(without_handlers, quote=False)


def strip_prompt_injection_markers(text: str) -> tuple[str, list[str]]:
    """Remove common injection phrases before model-assisted extraction."""
    warnings: list[str] = []
    cleaned = text
    lower = text.lower()
    for marker in INJECTION_MARKERS:
        if marker in lower:
            warnings.append(f"prompt_injection_marker_removed:{marker}")
            pattern = re.compile(re.escape(marker), re.IGNORECASE)
            cleaned = pattern.sub("[filtered]", cleaned)
    return cleaned, warnings


def enforce_fetch_limits(
    *,
    content_type: str | None,
    content_length: int | None,
    body_size: int,
    max_bytes: int,
    allowed_content_types: frozenset[str],
) -> None:
    if content_length is not None and content_length > max_bytes:
        raise UnsafeContentError("content-length exceeds limit")
    if body_size > max_bytes:
        raise UnsafeContentError("response body exceeds limit")
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime not in allowed_content_types:
            raise UnsafeContentError(f"disallowed content-type: {mime}")
