"""Application settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    plausible_domain: str
    plausible_api_key: str
    analytics_environment: str
    admin_username: str
    admin_password_hash: str
    admin_session_secret: str
    brief_price_cents: int = 20_000
    admin_session_ttl_seconds: int = 86_400
    admin_login_rate_limit: int = 5
    admin_login_rate_window_seconds: int = 900
    admin_login_lockout_seconds: int = 900
    admin_trust_proxy_headers: bool = False
    admin_trusted_proxy_cidrs: str = ""
    audit_page_size: int = 50
    brief_page_size: int = 50

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
    def admin_preview_mode(self) -> bool:
        flag = os.environ.get("ADMIN_PREVIEW_MODE", "").lower()
        return flag in ("1", "true", "yes")

    @property
    def admin_auth_configured(self) -> bool:
        creds = bool(
            self.admin_username
            and self.admin_password_hash
            and self.admin_session_secret
        )
        if self.admin_preview_mode:
            return creds
        return bool(self.database_url and creds)

    @property
    def admin_preview_enabled(self) -> bool:
        """True when ADMIN_PREVIEW_MODE is on and BASE_URL is not production.

        Hard-refuses saberistic.com so a mis-set env cannot open /admin without
        login in production.
        """
        if not self.admin_preview_mode:
            return False
        base = (self.base_url or "").lower()
        if "saberistic.com" in base:
            return False
        return True

    @property
    def analytics_enabled(self) -> bool:
        """True only when explicitly enabled and a Plausible domain is set."""
        flag = os.environ.get("ANALYTICS_ENABLED", "").lower()
        if flag not in ("1", "true", "yes"):
            return False
        return bool(self.plausible_domain)


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        stripe_publishable_key=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        resend_api_key=os.environ.get("RESEND_API_KEY", ""),
        from_email=os.environ.get("FROM_EMAIL", "noreply@saberistic.com"),
        notify_email=os.environ.get("NOTIFY_EMAIL", "inbox@saberistic.com"),
        base_url=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        plausible_domain=os.environ.get("PLAUSIBLE_DOMAIN", "").strip(),
        plausible_api_key=os.environ.get("PLAUSIBLE_API_KEY", "").strip(),
        analytics_environment=os.environ.get("ANALYTICS_ENV", "development").strip()
        or "development",
        admin_username=os.environ.get("ADMIN_USERNAME", "").strip(),
        admin_password_hash=os.environ.get("ADMIN_PASSWORD_HASH", "").strip(),
        admin_session_secret=os.environ.get("ADMIN_SESSION_SECRET", "").strip(),
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
        admin_trust_proxy_headers=os.environ.get(
            "ADMIN_TRUST_PROXY_HEADERS", ""
        ).lower()
        in ("1", "true", "yes"),
        admin_trusted_proxy_cidrs=os.environ.get("ADMIN_TRUSTED_PROXY_CIDRS", "").strip(),
    )
