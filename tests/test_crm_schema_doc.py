"""Guard docs/CRM_SCHEMA.md against pipeline-column and migration-ledger drift (#277)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.migrations.definitions import MIGRATIONS

CRM_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "CRM_SCHEMA.md"

CANONICAL_PIPELINE_COLUMNS = (
    "pipeline_owner",
    "expected_value_cents",
    "pipeline_loss_reason",
    "pipeline_nurture_reason",
)

LEGACY_PIPELINE_COLUMNS = (
    "owner",
    "expected_value",
    "stage_reason",
)

REQUIRED_MIGRATION_VERSIONS = tuple(f"{index:03d}" for index in range(1, 17))

PAYMENT_DETAIL_COLUMNS = (
    "payment_subtotal_cents",
    "payment_discount_cents",
    "payment_amount_cents",
    "payment_currency",
    "stripe_promotion_code_id",
    "stripe_coupon_id",
)


def _read_crm_schema() -> str:
    return CRM_SCHEMA_PATH.read_text(encoding="utf-8")


def _companies_table_section(text: str) -> str:
    """Markdown between ``### `companies` `` and the next ``### `` heading."""
    match = re.search(
        r"### `companies`\s*\n(.*?)(?=\n### |\n#### Legacy pipeline compatibility|\Z)",
        text,
        re.DOTALL,
    )
    assert match is not None, "companies table section missing from CRM_SCHEMA.md"
    return match.group(1)


def _migration_ledger_versions(text: str) -> list[str]:
    versions: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\| `(?P<version>\d{3})` \|", line)
        if match:
            versions.append(match.group("version"))
    return versions


@pytest.mark.unit
def test_crm_schema_file_exists() -> None:
    assert CRM_SCHEMA_PATH.is_file()


@pytest.mark.unit
def test_companies_table_documents_canonical_pipeline_columns() -> None:
    section = _companies_table_section(_read_crm_schema())
    for column in CANONICAL_PIPELINE_COLUMNS:
        assert f"| `{column}` |" in section, f"missing canonical column {column}"


@pytest.mark.unit
def test_companies_table_does_not_list_legacy_pipeline_columns_as_canonical() -> None:
    section = _companies_table_section(_read_crm_schema())
    for column in LEGACY_PIPELINE_COLUMNS:
        assert f"| `{column}` |" not in section, (
            f"legacy column {column} must not appear in canonical companies table; "
            "document it under legacy compatibility artifacts instead"
        )


@pytest.mark.unit
def test_crm_schema_labels_legacy_pipeline_artifacts() -> None:
    text = _read_crm_schema()
    assert "Legacy pipeline compatibility artifacts" in text
    for column in LEGACY_PIPELINE_COLUMNS:
        assert f"| `{column}` |" in text
    assert "`company_stage_history`" in text
    assert "compatibility" in text.lower()


@pytest.mark.unit
def test_migration_ledger_covers_001_through_016_without_gaps() -> None:
    text = _read_crm_schema()
    ledger_versions = _migration_ledger_versions(text)
    for version in REQUIRED_MIGRATION_VERSIONS:
        assert version in ledger_versions, f"migration {version} missing from ledger"

    ledger_001_016 = [v for v in ledger_versions if v in REQUIRED_MIGRATION_VERSIONS]
    assert ledger_001_016 == list(REQUIRED_MIGRATION_VERSIONS)


@pytest.mark.unit
def test_migration_ledger_names_match_definitions_through_016() -> None:
    text = _read_crm_schema()
    by_version = {m.version: m.name for m in MIGRATIONS}
    for version in REQUIRED_MIGRATION_VERSIONS:
        expected_name = by_version[version]
        pattern = rf"^\| `{version}` \| `{re.escape(expected_name)}` \|"
        assert re.search(pattern, text, re.MULTILINE), (
            f"ledger row for {version} must name migration {expected_name!r}"
        )


@pytest.mark.unit
def test_migration_015_and_016_have_reconciliation_sections() -> None:
    text = _read_crm_schema()
    assert "#### Migration `015`" in text
    assert "legacy pipeline reconciliation" in text.lower()
    assert "FROZEN_MIGRATION_DIGESTS" in text
    assert "ON CONFLICT (id) DO NOTHING" in text
    assert "#### Migration `016`" in text
    assert "project brief payment details" in text.lower()


@pytest.mark.unit
def test_project_briefs_documents_payment_detail_columns() -> None:
    text = _read_crm_schema()
    assert "### `project_briefs`" in text
    for column in PAYMENT_DETAIL_COLUMNS:
        assert f"| `{column}` |" in text


@pytest.mark.unit
def test_crm_schema_does_not_recommend_legacy_pipeline_columns_in_examples() -> None:
    text = _read_crm_schema()
    # Legacy names may appear only where reconciliation/compatibility is documented.
    without_allowed = text
    for pattern in (
        r"#### Legacy pipeline compatibility artifacts.*?(?=\n### |\n## |\Z)",
        r"#### Migration `015`.*?(?=\n#### Migration `016`|\n### |\n## |\Z)",
    ):
        without_allowed = re.sub(pattern, "", without_allowed, flags=re.DOTALL)
    for column in LEGACY_PIPELINE_COLUMNS:
        assert re.search(rf"\b{re.escape(column)}\b", without_allowed) is None, (
            f"legacy identifier {column} appears outside allowed compatibility docs"
        )
    assert "company_stage_history" not in without_allowed
    assert "idx_companies_owner" not in without_allowed
