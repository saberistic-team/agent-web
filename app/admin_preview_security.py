"""Admin preview authentication bypass guards (issue #330)."""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlunparse, urlparse

from app.app_environment import PREVIEW_AUTH_ALLOWED_ENVIRONMENTS, AppEnvironment

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


class AdminPreviewConfigError(ValueError):
    """Raised when admin preview configuration is unsafe or incomplete."""


class LoopbackOriginError(ValueError):
    """Raised when ``BASE_URL`` is not a validated loopback origin."""


def _normalize_hostname(host: str) -> str:
    trimmed = host.strip().rstrip(".")
    if trimmed.startswith("[") and trimmed.endswith("]"):
        trimmed = trimmed[1:-1]
    return trimmed.lower()


def _is_exact_localhost(host: str) -> bool:
    return _normalize_hostname(host) == "localhost"


def _loopback_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    normalized = _normalize_hostname(host)
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return None
    return ip if ip.is_loopback else None


def validate_loopback_base_url(base_url: str) -> str:
    """Parse and positively validate ``BASE_URL`` as a loopback HTTP origin.

    Accepts ``localhost``, ``127.0.0.0/8`` literals, and ``::1`` (including
    bracketed IPv6). Rejects lookalike hostnames, credentials, fragments,
    wildcards, non-HTTP(S) schemes, and DNS names that merely resolve to loopback.
    """
    raw = (base_url or "").strip()
    if not raw:
        raise LoopbackOriginError("BASE_URL is required")

    if "#" in raw:
        raise LoopbackOriginError("BASE_URL must not include a fragment")

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise LoopbackOriginError("BASE_URL is malformed") from exc

    if parsed.scheme not in ("http", "https"):
        raise LoopbackOriginError("BASE_URL scheme must be http or https")
    if parsed.username or parsed.password:
        raise LoopbackOriginError("BASE_URL must not include userinfo")

    host = parsed.hostname
    if not host:
        raise LoopbackOriginError("BASE_URL must include an explicit host")
    if "*" in host:
        raise LoopbackOriginError("BASE_URL must not use a wildcard host")

    normalized_host = _normalize_hostname(host)
    if normalized_host != "localhost" and _loopback_ip(normalized_host) is None:
        raise LoopbackOriginError(
            "BASE_URL host must be localhost or a loopback IP literal"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise LoopbackOriginError("BASE_URL port is out of range") from exc

    if port is not None and not (1 <= port <= 65535):
        raise LoopbackOriginError("BASE_URL port is out of range")

    host_port = raw.split("://", 1)[-1].split("/", 1)[0]
    if host_port.endswith(":"):
        raise LoopbackOriginError("BASE_URL port is malformed")

    loopback_ip = _loopback_ip(normalized_host)
    if loopback_ip is not None and isinstance(loopback_ip, ipaddress.IPv6Address):
        netloc_host = f"[{loopback_ip}]"
    else:
        netloc_host = normalized_host

    netloc = netloc_host if port is None else f"{netloc_host}:{port}"
    normalized = urlunparse((parsed.scheme, netloc, "", "", "", "")).rstrip("/")
    return normalized


def is_loopback_bind_host(host: str) -> bool:
    """Return True when ``host`` binds only to a loopback interface."""
    trimmed = (host or "").strip()
    if not trimmed:
        return False
    if trimmed in {"0.0.0.0", "::", "[::]"}:
        return False
    if _is_exact_localhost(trimmed):
        return True
    return _loopback_ip(trimmed) is not None


def validate_preview_server_bind(host: str) -> None:
    """Contract for preview launchers: refuse public or unspecified bind addresses."""
    trimmed = (host or "").strip()
    if not trimmed:
        raise AdminPreviewConfigError(
            "SERVER_BIND_HOST is required when ADMIN_PREVIEW_MODE is enabled"
        )
    if trimmed in {"0.0.0.0", "::", "[::]"}:
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE cannot bind to a public interface"
        )
    if not is_loopback_bind_host(trimmed):
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE requires SERVER_BIND_HOST to be loopback "
            "(127.0.0.1, ::1, or another loopback literal)"
        )


def validate_preview_database_isolation(settings: Settings) -> None:
    """Minimal database guard until #331 lands; blocks obvious production URLs."""
    if not settings.admin_preview_mode:
        return
    url = settings.database_url.strip().lower()
    if not url:
        return
    prohibited = ("render.com", ".saberistic.com")
    if any(marker in url for marker in prohibited):
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE cannot use a production DATABASE_URL"
        )


def compute_admin_preview_enabled(
    *,
    admin_preview_mode: bool,
    app_environment: AppEnvironment,
    base_url: str,
    server_bind_host: str,
) -> bool:
    """Return whether the auth bypass is active after positive validation."""
    if not admin_preview_mode:
        return False
    if app_environment not in PREVIEW_AUTH_ALLOWED_ENVIRONMENTS:
        return False
    try:
        validate_loopback_base_url(base_url)
    except LoopbackOriginError:
        return False
    if not is_loopback_bind_host(server_bind_host):
        return False
    return True


def validate_admin_preview_config(settings: Settings) -> None:
    """Fail startup when preview mode is requested but any invariant fails."""
    if not settings.admin_preview_mode:
        return

    if settings.app_environment not in PREVIEW_AUTH_ALLOWED_ENVIRONMENTS:
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE requires APP_ENV development or preview "
            f"(got {settings.app_environment.value})"
        )

    try:
        validate_loopback_base_url(settings.base_url)
    except LoopbackOriginError as exc:
        raise AdminPreviewConfigError(str(exc)) from exc

    validate_preview_server_bind(settings.server_bind_host)
    validate_preview_database_isolation(settings)

    if not settings.admin_preview_enabled:
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE configuration failed closed validation"
        )


def log_admin_preview_posture(settings: Settings) -> None:
    """Emit redacted startup diagnostics for the effective preview posture."""
    loopback_only = settings.admin_preview_enabled
    logger.info(
        "admin_preview_posture admin_preview_enabled=%s loopback_only=%s app_env=%s",
        settings.admin_preview_enabled,
        loopback_only,
        settings.app_environment.value,
    )
