"""Deterministic CRM schema documentation contract (#277).

Compares ``docs/CRM_SCHEMA.md`` against canonical migration definitions
(``001``–``016``) so pipeline-column and migration-ledger regressions are
caught in CI without a live database.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.migrations.definitions import MIGRATIONS

CRM_SCHEMA_DOC = Path(__file__).resolve().parent.parent / "docs" / "CRM_SCHEMA.md"

MIGRATION_LEDGER_LAST = "016"

CANONICAL_COMPANY_PIPELINE_COLUMNS: frozenset[str] = frozenset(
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

LEGACY_COMPANY_PIPELINE_COLUMNS: frozenset[str] = frozenset(
    {"owner", "expected_value", "stage_reason"}
)

LEGACY_PIPELINE_TABLES: frozenset[str] = frozenset({"company_stage_history"})

# Columns on ``companies`` after migrations 001–016 (fresh install).
COMPANIES_COLUMNS_THROUGH_016: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        "name",
        "website",
        "status",
        "domain",
        "category",
        "stage",
        "headcount_estimate",
        "funding_summary",
        "target_status",
        "last_verified_at",
        "notes",
        "archived_at",
    }
) | CANONICAL_COMPANY_PIPELINE_COLUMNS

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

_MIGRATION_TABLE_ROW = re.compile(
    r"^\|\s*`(\d{3})`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$",
    re.MULTILINE,
)
_TABLE_COLUMN = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)
_SECTION = re.compile(r"^### `([^`]+)`\s*$", re.MULTILINE)
_LEGACY_SECTION = re.compile(
    r"^## Legacy compatibility\b|^### Legacy compatibility\b",
    re.MULTILINE | re.IGNORECASE,
)
_LEGACY_PIPELINE_REF = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in sorted(LEGACY_COMPANY_PIPELINE_COLUMNS))
    + r")\b"
)
_LEGACY_TABLE_REF = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in LEGACY_PIPELINE_TABLES) + r")\b"
)
_SECTION_H2 = re.compile(r"^## (.+)$", re.MULTILINE)


def migrations_through(version: str) -> tuple:
    selected = []
    for migration in MIGRATIONS:
        selected.append(migration)
        if migration.version == version:
            return tuple(selected)
    raise ValueError(f"migration {version} not found")


def expected_migration_ledger() -> dict[str, str]:
    """Return version → migration name for ``001`` through ``MIGRATION_LEDGER_LAST``."""
    ledger: dict[str, str] = {}
    for migration in MIGRATIONS:
        ledger[migration.version] = migration.name
        if migration.version == MIGRATION_LEDGER_LAST:
            break
    return ledger


def _section_bounds(text: str, heading: str) -> tuple[int, int]:
    sections: list[tuple[str, int]] = []
    for section_match in _SECTION.finditer(text):
        sections.append((section_match.group(1), section_match.start()))
    for index, (name, start) in enumerate(sections):
        if name != heading:
            continue
        end = sections[index + 1][1] if index + 1 < len(sections) else len(text)
        return start, end
    raise KeyError(f"section `{heading}` not found")


def _h2_section_bounds(text: str, heading: str) -> tuple[int, int]:
    sections: list[tuple[str, int]] = []
    for match in _SECTION_H2.finditer(text):
        sections.append((match.group(1).strip(), match.start()))
    for index, (name, start) in enumerate(sections):
        if name != heading:
            continue
        end = sections[index + 1][1] if index + 1 < len(sections) else len(text)
        return start, end
    raise KeyError(f"section `{heading}` not found")


def _legacy_section_start(text: str) -> int | None:
    match = _LEGACY_SECTION.search(text)
    return match.start() if match else None


def _table_columns(section_text: str) -> set[str]:
    columns: set[str] = set()
    for line in section_text.splitlines():
        if not line.startswith("|"):
            continue
        if line.startswith("| Column") or line.startswith("|--------"):
            continue
        match = _TABLE_COLUMN.match(line)
        if match:
            columns.add(match.group(1))
    return columns


def _migration_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for match in _MIGRATION_TABLE_ROW.finditer(text):
        version, name, purpose = match.groups()
        rows[version] = (name, purpose.strip())
    return rows


def validate_crm_schema_doc(path: Path | None = None) -> list[str]:
    """Return human-readable errors; empty list means the contract passes."""
    doc_path = path or CRM_SCHEMA_DOC
    text = doc_path.read_text(encoding="utf-8")
    errors: list[str] = []

    expected_ledger = expected_migration_ledger()
    documented = _migration_rows(text)
    for version, expected_name in expected_ledger.items():
        if version not in documented:
            errors.append(
                f"migration ledger missing version `{version}` "
                f"(expected name `{expected_name}`)"
            )
            continue
        documented_name, _ = documented[version]
        if documented_name != expected_name:
            errors.append(
                f"migration `{version}` documents name `{documented_name}`; "
                f"expected `{expected_name}` from definitions.py"
            )

    extra_versions = sorted(
        version for version in documented if version <= MIGRATION_LEDGER_LAST and version not in expected_ledger
    )
    if extra_versions:
        errors.append(
            f"migration ledger documents unknown baseline versions: {extra_versions}"
        )

    try:
        companies_start, companies_end = _section_bounds(text, "companies")
        companies_section = text[companies_start:companies_end]
    except KeyError as exc:
        errors.append(str(exc))
        companies_section = ""
        companies_start = companies_end = 0

    if companies_section:
        documented_columns = _table_columns(companies_section)
        missing_pipeline = CANONICAL_COMPANY_PIPELINE_COLUMNS - documented_columns
        if missing_pipeline:
            errors.append(
                "companies table missing canonical pipeline columns in CRM_SCHEMA.md: "
                + ", ".join(sorted(missing_pipeline))
            )
        legacy_in_canonical = LEGACY_COMPANY_PIPELINE_COLUMNS & documented_columns
        if legacy_in_canonical:
            errors.append(
                "companies table documents legacy pipeline columns as canonical: "
                + ", ".join(sorted(legacy_in_canonical))
            )

    legacy_start = _legacy_section_start(text)
    if legacy_start is None:
        errors.append("missing `Legacy compatibility` section for migration 015 artifacts")
    else:
        legacy_section = text[legacy_start:]
        for column in LEGACY_COMPANY_PIPELINE_COLUMNS:
            if f"`{column}`" not in legacy_section:
                errors.append(
                    f"legacy compatibility section missing column `{column}`"
                )
        for table in LEGACY_PIPELINE_TABLES:
            if f"`{table}`" not in legacy_section:
                errors.append(
                    f"legacy compatibility section missing table `{table}`"
                )

    try:
        brief_start, brief_end = _section_bounds(text, "project_briefs")
        brief_section = text[brief_start:brief_end]
    except KeyError:
        brief_section = ""

    if brief_section:
        documented_payment = _table_columns(brief_section) & PROJECT_BRIEF_PAYMENT_COLUMNS
        missing_payment = PROJECT_BRIEF_PAYMENT_COLUMNS - documented_payment
        if missing_payment:
            errors.append(
                "project_briefs table missing payment columns from migration 016: "
                + ", ".join(sorted(missing_payment))
            )

    # Operational prose outside migrations/legacy/companies sections must not
    # recommend legacy ids (companies may mention them in "do not query" guidance).
    skip_regions: list[tuple[int, int]] = []
    if legacy_start is not None:
        skip_regions.append((legacy_start, len(text)))
    try:
        migrations_start, migrations_end = _h2_section_bounds(text, "Migrations")
        skip_regions.append((migrations_start, migrations_end))
    except KeyError:
        pass
    if companies_section:
        skip_regions.append((companies_start, companies_end))

    def _in_skip_region(offset: int) -> bool:
        return any(start <= offset < end for start, end in skip_regions)

    for match in _LEGACY_PIPELINE_REF.finditer(text):
        if _in_skip_region(match.start()):
            continue
        errors.append(
            f"legacy pipeline identifier `{match.group(1)}` appears outside the "
            "compatibility and migration-015 documentation sections"
        )

    for match in _LEGACY_TABLE_REF.finditer(text):
        if _in_skip_region(match.start()):
            continue
        errors.append(
            f"legacy pipeline table `{match.group(1)}` appears outside the "
            "compatibility and migration-015 documentation sections"
        )
    if "`015`" not in text or "reconcile_acquisition_pipeline_schema" not in text:
        errors.append("migration 015 reconciliation is not documented")
    if "`016`" not in text or "project_brief_payment_details" not in text:
        errors.append("migration 016 project-brief payment columns are not documented")

    if "expected_value_cents" not in text:
        errors.append("documentation never mentions canonical `expected_value_cents`")
    if "INTEGER" not in text or "cents" not in text.lower():
        errors.append("documentation does not describe monetary units in cents")

    return errors
