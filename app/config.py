"""Environment-backed settings for project brief flow."""

from __future__ import annotations

import os

BRIEF_PRICE_CENTS = 20_000


def stripe_secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "")


def stripe_webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "https://saberistic.com").rstrip("/")


def resend_api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "")


def notify_email() -> str:
    return os.environ.get("NOTIFY_EMAIL", "inbox@saberistic.com")


def from_email() -> str:
    return os.environ.get("FROM_EMAIL", "noreply@saberistic.com")


def database_url() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw:
        if raw.startswith("postgres://"):
            return raw.replace("postgres://", "postgresql://", 1)
        return raw
    return "sqlite:///./project_briefs.db"
