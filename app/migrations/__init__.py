"""Versioned Postgres migrations applied at startup."""

from app.migrations.runner import apply_migrations

__all__ = ["apply_migrations"]
