"""Migration tests for project brief payment amount columns (#197)."""

from __future__ import annotations

import pytest

from app.migrations.definitions import MIGRATIONS


@pytest.mark.unit
def test_project_briefs_payment_amounts_migration_is_idempotent() -> None:
    migration = next(m for m in MIGRATIONS if m.name == "project_briefs_payment_amounts")
    assert migration.version == "016"
    for column in (
        "payment_subtotal_cents",
        "payment_discount_cents",
        "payment_amount_cents",
        "payment_currency",
        "stripe_discount_id",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in migration.up_sql


@pytest.mark.unit
def test_payment_columns_are_nullable_for_legacy_rows() -> None:
    migration = next(m for m in MIGRATIONS if m.name == "project_briefs_payment_amounts")
    for column in (
        "payment_subtotal_cents INTEGER",
        "payment_discount_cents INTEGER",
        "payment_amount_cents INTEGER",
        "payment_currency TEXT",
        "stripe_discount_id TEXT",
    ):
        assert column in migration.up_sql
    assert "NOT NULL" not in migration.up_sql
