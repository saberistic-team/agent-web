"""Unit tests for CRM schema documentation contract (#277)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.crm_schema_doc_contract import (
    CANONICAL_COMPANY_PIPELINE_COLUMNS,
    COMPANIES_COLUMNS_THROUGH_016,
    CRM_SCHEMA_DOC,
    MIGRATION_LEDGER_LAST,
    PROJECT_BRIEF_PAYMENT_COLUMNS,
    expected_migration_ledger,
    validate_crm_schema_doc,
)
from scripts.check_crm_schema_docs import main as check_crm_schema_docs_main


@pytest.mark.unit
def test_crm_schema_doc_passes_contract() -> None:
    errors = validate_crm_schema_doc(CRM_SCHEMA_DOC)
    assert errors == []


@pytest.mark.unit
def test_check_crm_schema_docs_script_exits_zero() -> None:
    assert check_crm_schema_docs_main() == 0


@pytest.mark.unit
def test_expected_migration_ledger_covers_001_through_016_without_gaps() -> None:
    ledger = expected_migration_ledger()
    assert ledger["001"] == "project_briefs"
    assert ledger[MIGRATION_LEDGER_LAST] == "project_brief_payment_details"
    versions = sorted(ledger)
    assert versions == [f"{index:03d}" for index in range(1, 17)]


@pytest.mark.unit
def test_contract_flags_missing_canonical_pipeline_column(tmp_path: Path) -> None:
    text = CRM_SCHEMA_DOC.read_text(encoding="utf-8")
    broken = text.replace("`pipeline_owner`", "`assigned_operator`")
    path = tmp_path / "CRM_SCHEMA.md"
    path.write_text(broken, encoding="utf-8")
    errors = validate_crm_schema_doc(path)
    assert any("pipeline_owner" in error for error in errors)


@pytest.mark.unit
def test_contract_flags_legacy_column_in_companies_table(tmp_path: Path) -> None:
    text = CRM_SCHEMA_DOC.read_text(encoding="utf-8")
    broken = text.replace(
        "| `pipeline_owner` | `TEXT` | Optional assigned operator username |",
        "| `owner` | `TEXT` | Assigned operator |",
    )
    path = tmp_path / "CRM_SCHEMA.md"
    path.write_text(broken, encoding="utf-8")
    errors = validate_crm_schema_doc(path)
    assert any("legacy pipeline columns as canonical" in error for error in errors)


@pytest.mark.unit
def test_canonical_pipeline_column_set_is_stable() -> None:
    assert CANONICAL_COMPANY_PIPELINE_COLUMNS == frozenset(
        {
            "pipeline_stage",
            "next_action",
            "next_action_due_at",
            "pipeline_owner",
            "expected_value_cents",
            "pipeline_loss_reason",
            "pipeline_nurture_reason",
        }
    )
    assert CANONICAL_COMPANY_PIPELINE_COLUMNS.issubset(COMPANIES_COLUMNS_THROUGH_016)
    assert PROJECT_BRIEF_PAYMENT_COLUMNS
