"""Admin preview authentication bypass guards (loopback-only, startup-fixed)."""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

from app.app_environment import PREVIEW_ALLOWED_ENVIRONMENTS, AppEnvironment

logger = logging.getLogger(__name__)


class AdminPreviewConfigError(ValueError):
    """Raised when admin preview mode is enabled with an unsafe configuration."""


def _normalize_hostname(hostname: str) -> str:
    return hostname.strip().lower().rstrip(".")


def is_loopback_origin(base_url: str) -> bool:
    """Return True when ``base_url`` is a positively validated loopback HTTP origin."""
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.params or parsed.query or parsed.fragment:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and not (1 <= port <= 65535):
        return False

    normalized = _normalize_hostname(hostname)
    if normalized == "localhost":
        return True

    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_loopback_bind_host(host: str) -> bool:
    """Return True when ``host`` is a loopback bind address (not public/wildcard)."""
    raw = (host or "").strip()
    if not raw:
        return False
    if raw in {"0.0.0.0", "::"}:
        return False

    candidate = raw
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]

    if _normalize_hostname(candidate) == "localhost":
        return True

    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def validate_preview_bind_host(host: str) -> None:
    """Contract for preview launchers: refuse public or wildcard bind addresses."""
    if not is_loopback_bind_host(host):
        raise AdminPreviewConfigError(
            "preview server bind address must be a loopback interface "
            f"(got {host!r})"
        )


def validate_admin_preview_config(
    *,
    admin_preview_mode: bool,
    app_environment: AppEnvironment,
    base_url: str,
    server_bind_host: str,
    admin_trusted_proxy_cidrs: str = "",
    admin_trusted_edge_cidrs: str = "",
) -> None:
    """Fail closed before serving when preview bypass is requested unsafely."""
    if not admin_preview_mode:
        return

    if app_environment not in PREVIEW_ALLOWED_ENVIRONMENTS:
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE requires APP_ENV=development or preview, "
            f"not {app_environment.value}"
        )

    if not is_loopback_origin(base_url):
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE requires BASE_URL to be a validated loopback origin"
        )

    if not server_bind_host.strip():
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE requires SERVER_BIND_HOST to be set to a loopback address"
        )

    validate_preview_bind_host(server_bind_host)

    if admin_trusted_proxy_cidrs.strip() or admin_trusted_edge_cidrs.strip():
        raise AdminPreviewConfigError(
            "ADMIN_PREVIEW_MODE is incompatible with production proxy CIDR configuration"
        )


def resolve_admin_preview_enabled(
    *,
    admin_preview_mode: bool,
    app_environment: AppEnvironment,
    base_url: str,
    server_bind_host: str,
    admin_trusted_proxy_cidrs: str = "",
    admin_trusted_edge_cidrs: str = "",
) -> bool:
    """Compute whether the auth bypass is active after startup validation."""
    if not admin_preview_mode:
        return False
    validate_admin_preview_config(
        admin_preview_mode=admin_preview_mode,
        app_environment=app_environment,
        base_url=base_url,
        server_bind_host=server_bind_host,
        admin_trusted_proxy_cidrs=admin_trusted_proxy_cidrs,
        admin_trusted_edge_cidrs=admin_trusted_edge_cidrs,
    )
    return True


def log_admin_preview_posture(*, admin_preview_enabled: bool) -> None:
    """Emit redacted startup diagnostics for the effective preview posture."""
    logger.info(
        "admin_preview_enabled=%s, loopback_only=true",
        "true" if admin_preview_enabled else "false",
    )
