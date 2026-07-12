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
    brief_price_cents: int = 20_000

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def stripe_configured(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def email_configured(self) -> bool:
        return bool(self.resend_api_key)


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
    )
