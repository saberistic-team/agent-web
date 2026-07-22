#!/usr/bin/env python3
"""Validate docs/CRM_SCHEMA.md against canonical migrations 001-016 (#277)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

ROOT = _ROOT
CRM_SCHEMA_PATH = ROOT / "docs" / "CRM_SCHEMA.md"

CANONICAL_PIPELINE_COLUMNS: frozenset[str] = frozenset(
    {
        "pipeline_owner",
        "expected_value_cents",
        "pipeline_loss_reason",
        "pipeline_nurture_reason",
    }
)
LEGACY_PIPELINE_COLUMNS: frozenset[str] = frozenset(
    {
        "owner",
        "expected_value",
        "stage_reason",
    }
)
PROJECT_BRIEF_PAYMENT_COLUMNS: frozenset[str] = frozenset(
    {
        "payment_subtotal_cents",
        "payment_discount_cents",
        "payment_amount_cents",
        "payment_currency",
        "stripe_promotion_code_id",
        "stripe_coupon_id",
    }
)
LEDGER_MAX_VERSION = "016"
MIGRATION_ROW_RE = re.compile(
    r"^\|\s*`(\d{3})`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$"
)
TABLE_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def load_crm_schema_doc(path: Path = CRM_SCHEMA_PATH) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    marker = f"### `{heading}`"
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    match = re.search(r"\n### `(?!#)", rest)
    end = match.start() if match else len(rest)
    return rest[:end]


def migration_ledger_entries(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    in_ledger = False
    for line in text.splitlines():
        if line.startswith("| Version | Name | Purpose |"):
            in_ledger = True
            continue
        if in_ledger:
            if not line.startswith("| `"):
                if entries:
                    break
                continue
            match = MIGRATION_ROW_RE.match(line)
            if match is None:
                continue
            entries[match.group(1)] = match.group(2)
    return entries


def validate_canonical_pipeline_columns(companies_section: str) -> list[str]:
    errors: list[str] = []
    canonical_block = companies_section.split("#### Legacy pipeline compatibility", 1)[0]
    for column in sorted(CANONICAL_PIPELINE_COLUMNS):
        if f"`{column}`" not in canonical_block:
            errors.append(
                f"companies table missing canonical pipeline column `{column}` "
                f"in docs/CRM_SCHEMA.md"
            )
    for column in sorted(LEGACY_PIPELINE_COLUMNS):
        if TABLE_ROW_RE.search(_line_for_column(canonical_block, column) or ""):
            errors.append(
                f"companies table documents legacy pipeline column `{column}` "
                f"outside the compatibility section"
            )
    return errors


def _line_for_column(block: str, column: str) -> str | None:
    for line in block.splitlines():
        if f"`{column}`" in line and line.strip().startswith("|"):
            return line
    return None


def validate_legacy_compatibility_section(text: str) -> list[str]:
    errors: list[str] = []
    marker = "#### Legacy pipeline compatibility"
    if marker not in text:
        return ["docs/CRM_SCHEMA.md missing legacy pipeline compatibility section"]
    legacy_block = text.split(marker, 1)[1].split("\n### ", 1)[0]
    for column in sorted(LEGACY_PIPELINE_COLUMNS):
        if f"`{column}`" not in legacy_block:
            errors.append(
                f"legacy compatibility section missing artifact `{column}` "
                f"in docs/CRM_SCHEMA.md"
            )
    if "`company_stage_history`" not in legacy_block:
        errors.append(
            "legacy compatibility section missing `company_stage_history` "
            "in docs/CRM_SCHEMA.md"
        )
    return errors


def validate_project_brief_payment_columns(text: str) -> list[str]:
    errors: list[str] = []
    brief_section = _section(text, "project_briefs")
    if not brief_section:
        return ["docs/CRM_SCHEMA.md missing ### `project_briefs` section"]
    for column in sorted(PROJECT_BRIEF_PAYMENT_COLUMNS):
        if f"`{column}`" not in brief_section:
            errors.append(
                f"project_briefs section missing payment column `{column}` "
                f"in docs/CRM_SCHEMA.md"
            )
    return errors


def validate_migration_ledger(text: str) -> list[str]:
    from app.migrations.definitions import MIGRATIONS

    errors: list[str] = []
    documented = migration_ledger_entries(text)
    expected = [m for m in MIGRATIONS if m.version <= LEDGER_MAX_VERSION]
    expected_versions = [m.version for m in expected]

    if list(documented) != expected_versions:
        missing = [v for v in expected_versions if v not in documented]
        extra = [v for v in documented if v not in expected_versions]
        if missing:
            errors.append(
                "migration ledger missing versions: " + ", ".join(missing)
            )
        if extra:
            errors.append(
                "migration ledger documents unexpected versions: "
                + ", ".join(extra)
            )
        if documented and list(documented) != expected_versions:
            errors.append(
                "migration ledger versions are not contiguous 001-016 without gaps"
            )

    for migration in expected:
        documented_name = documented.get(migration.version)
        if documented_name is None:
            continue
        if documented_name != migration.name:
            errors.append(
                f"migration `{migration.version}` documented as `{documented_name}` "
                f"but definitions.py uses `{migration.name}`"
            )

    if "#### Migration `015`" not in text:
        errors.append("docs/CRM_SCHEMA.md missing Migration `015` reconciliation section")
    if "#### Migration `016`" not in text:
        errors.append("docs/CRM_SCHEMA.md missing Migration `016` payment section")
    return errors


def _strip_allowed_legacy_blocks(text: str) -> str:
    """Remove prose blocks that intentionally document legacy identifiers."""
    stripped = text
    legacy_start = stripped.find("#### Legacy pipeline compatibility")
    if legacy_start >= 0:
        legacy_end = stripped.find("\n### `", legacy_start + 1)
        if legacy_end < 0:
            legacy_end = len(stripped)
        stripped = stripped[:legacy_start] + stripped[legacy_end:]

    reconcile_start = stripped.find("#### Migration `015`")
    if reconcile_start >= 0:
        reconcile_end = stripped.find("#### Migration `016`", reconcile_start + 1)
        if reconcile_end < 0:
            reconcile_end = len(stripped)
        stripped = stripped[:reconcile_start] + stripped[reconcile_end:]
    return stripped


def validate_no_legacy_operational_queries(text: str) -> list[str]:
    """Legacy identifiers may appear only in compatibility / reconciliation prose."""
    errors: list[str] = []
    scrubbed = _strip_allowed_legacy_blocks(text)
    for number, line in enumerate(scrubbed.splitlines(), start=1):
        if "legacy" in line.lower() or "compatibility" in line.lower():
            continue
        for column in LEGACY_PIPELINE_COLUMNS:
            if re.search(rf"\b{re.escape(column)}\b", line):
                if column == "owner" and "pipeline_owner" in line:
                    continue
                if column == "expected_value" and "expected_value_cents" in line:
                    continue
                errors.append(
                    f"docs/CRM_SCHEMA.md:{number}: legacy pipeline identifier "
                    f"`{column}` outside compatibility/reconciliation sections"
                )
    return errors


def validate_crm_schema_doc(path: Path = CRM_SCHEMA_PATH) -> list[str]:
    text = load_crm_schema_doc(path)
    companies_section = _section(text, "companies")
    if not companies_section:
        return ["docs/CRM_SCHEMA.md missing ### `companies` section"]

    errors: list[str] = []
    errors.extend(validate_canonical_pipeline_columns(companies_section))
    errors.extend(validate_legacy_compatibility_section(text))
    errors.extend(validate_project_brief_payment_columns(text))
    errors.extend(validate_migration_ledger(text))
    errors.extend(validate_no_legacy_operational_queries(text))

    if "`pipeline_stage_history`" not in text:
        errors.append("docs/CRM_SCHEMA.md must document `pipeline_stage_history`")
    if "idx_companies_owner" in text:
        errors.append(
            "docs/CRM_SCHEMA.md references legacy index idx_companies_owner"
        )
    return errors


def main() -> int:
    errors = validate_crm_schema_doc()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("docs/CRM_SCHEMA.md matches canonical migrations 001-016")
    return 0


if __name__ == "__main__":
    sys.exit(main())
