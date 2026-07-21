"""Application settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.app_environment import AppEnvironment, parse_app_environment
from app.admin_preview_security import resolve_admin_preview_enabled


def _parse_admin_preview_mode(raw: str) -> bool:
    return raw.lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    database_url: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_publishable_key: str
    resend_api_key: str
    from_email: str
    notify_email: str
    base_url: str
    analytics_environment: str
    app_environment: AppEnvironment
    admin_username: str
    admin_password_hash: str
    admin_session_secret: str
    admin_login_limiter_secret: str = ""
    admin_login_limiter_previous_secret: str = ""
    admin_preview_mode: bool = False
    admin_preview_enabled: bool = False
    server_bind_host: str = ""
    brief_price_cents: int = 20_000
    admin_session_ttl_seconds: int = 86_400
    admin_login_rate_limit: int = 5
    admin_login_rate_window_seconds: int = 900
    admin_login_lockout_seconds: int = 900
    admin_trust_proxy_headers: bool = False
    admin_trusted_proxy_cidrs: str = ""
    admin_trusted_edge_cidrs: str = ""
    audit_page_size: int = 50
    brief_page_size: int = 50
    analytics_ingest_rate_limit: int = 60
    analytics_ingest_rate_window_seconds: int = 60
    analytics_ingest_lockout_seconds: int = 300

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def stripe_configured(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def email_configured(self) -> bool:
        return bool(self.resend_api_key)

    @property
    def admin_auth_configured(self) -> bool:
        creds = bool(
            self.admin_username
            and self.admin_password_hash
            and self.admin_session_secret
            and self.admin_login_limiter_secret
        )
        if self.admin_preview_mode:
            return creds
        return bool(self.database_url and creds)

    # admin_preview_enabled is a startup-fixed field (see
    # app.admin_preview_security.resolve_admin_preview_enabled), not a
    # property — computing it once in get_settings() means a mid-process
    # environment change can never flip this security-critical flag. This
    # supersedes an earlier, simpler main-branch property of the same name
    # that only checked ADMIN_PREVIEW_MODE + a saberistic.com base-URL
    # denylist; #330 additionally requires a validated loopback bind host
    # and no public-facing proxy/edge CIDRs.

    @property
    def first_party_analytics_enabled(self) -> bool:
        """True when first-party analytics is explicitly enabled."""
        for env_name in ("FIRST_PARTY_ANALYTICS_ENABLED", "ANALYTICS_ENABLED"):
            flag = os.environ.get(env_name, "").lower()
            if flag in ("1", "true", "yes"):
                return True
        return False


def get_settings() -> Settings:
    app_environment_raw = (
        os.environ.get("APP_ENV") or os.environ.get("ANALYTICS_ENV", "development")
    ).strip() or "development"
    app_environment = parse_app_environment(app_environment_raw)
    admin_preview_mode = _parse_admin_preview_mode(
        os.environ.get("ADMIN_PREVIEW_MODE", "")
    )
    base_url = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    server_bind_host = os.environ.get("SERVER_BIND_HOST", "").strip()
    admin_trusted_proxy_cidrs = os.environ.get("ADMIN_TRUSTED_PROXY_CIDRS", "").strip()
    admin_trusted_edge_cidrs = os.environ.get("ADMIN_TRUSTED_EDGE_CIDRS", "").strip()
    admin_preview_enabled = resolve_admin_preview_enabled(
        admin_preview_mode=admin_preview_mode,
        app_environment=app_environment,
        base_url=base_url,
        server_bind_host=server_bind_host,
        admin_trusted_proxy_cidrs=admin_trusted_proxy_cidrs,
        admin_trusted_edge_cidrs=admin_trusted_edge_cidrs,
    )
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        stripe_publishable_key=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        resend_api_key=os.environ.get("RESEND_API_KEY", ""),
        from_email=os.environ.get("FROM_EMAIL", "noreply@saberistic.com"),
        notify_email=os.environ.get("NOTIFY_EMAIL", "inbox@saberistic.com"),
        base_url=base_url,
        analytics_environment=app_environment_raw,
        app_environment=app_environment,
        admin_preview_mode=admin_preview_mode,
        admin_preview_enabled=admin_preview_enabled,
        server_bind_host=server_bind_host,
        admin_username=os.environ.get("ADMIN_USERNAME", "").strip(),
        admin_password_hash=os.environ.get("ADMIN_PASSWORD_HASH", "").strip(),
        admin_session_secret=os.environ.get("ADMIN_SESSION_SECRET", "").strip(),
        admin_login_limiter_secret=os.environ.get(
            "ADMIN_LOGIN_LIMITER_SECRET", ""
        ).strip(),
        admin_login_limiter_previous_secret=os.environ.get(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", ""
        ).strip(),
        admin_session_ttl_seconds=int(os.environ.get("ADMIN_SESSION_TTL_SECONDS", "86400")),
        admin_login_rate_limit=int(os.environ.get("ADMIN_LOGIN_RATE_LIMIT", "5")),
        admin_login_rate_window_seconds=int(
            os.environ.get("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
        ),
        admin_login_lockout_seconds=int(
            os.environ.get("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
        ),
        audit_page_size=int(os.environ.get("AUDIT_PAGE_SIZE", "50")),
        brief_page_size=int(os.environ.get("BRIEF_PAGE_SIZE", "50")),
        analytics_ingest_rate_limit=int(os.environ.get("ANALYTICS_INGEST_RATE_LIMIT", "60")),
        analytics_ingest_rate_window_seconds=int(
            os.environ.get("ANALYTICS_INGEST_RATE_WINDOW_SECONDS", "60")
        ),
        analytics_ingest_lockout_seconds=int(
            os.environ.get("ANALYTICS_INGEST_LOCKOUT_SECONDS", "300")
        ),
        admin_trust_proxy_headers=os.environ.get(
            "ADMIN_TRUST_PROXY_HEADERS", ""
        ).lower()
        in ("1", "true", "yes"),
        admin_trusted_proxy_cidrs=admin_trusted_proxy_cidrs,
        admin_trusted_edge_cidrs=admin_trusted_edge_cidrs,
    )
