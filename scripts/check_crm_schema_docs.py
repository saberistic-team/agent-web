#!/usr/bin/env python3
"""Validate docs/CRM_SCHEMA.md against canonical migrations 001–016 (#277)."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRM_SCHEMA_PATH = ROOT / "docs" / "CRM_SCHEMA.md"
DEFINITIONS_PATH = ROOT / "app" / "migrations" / "definitions.py"

CANONICAL_PIPELINE_COLUMNS = frozenset(
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

LEGACY_PIPELINE_COLUMNS = frozenset(
    {
        "owner",
        "expected_value",
        "stage_reason",
    }
)

PAYMENT_DETAIL_COLUMNS = frozenset(
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

COMPATIBILITY_HEADING = "#### Legacy pipeline compatibility artifacts"
MIGRATION_015_HEADING = "#### Migration `015`"
MIGRATION_016_HEADING = "#### Migration `016`"
MIGRATION_TABLE_HEADING = "## Migrations"


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    errors: tuple[str, ...]


def load_definitions() -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Load migration registry without importing app.migrations (avoids psycopg)."""
    spec = importlib.util.spec_from_file_location(
        "migration_definitions_crm_docs", DEFINITIONS_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {DEFINITIONS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    migrations = [(m.version, m.name) for m in module.MIGRATIONS]
    return migrations


def _section(text: str, heading: str, *, stop_at: str | None = None) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    body = text[start + len(heading) :]
    if stop_at:
        stop = body.find(stop_at)
        if stop >= 0:
            body = body[:stop]
    return body


def _table_column_names(section: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"^\|\s*`([^`]+)`\s*\|", section, flags=re.M):
        names.add(match.group(1))
    return names


def _migration_ledger_rows(text: str) -> dict[str, str]:
    section = _section(text, MIGRATION_TABLE_HEADING, stop_at=MIGRATION_015_HEADING)
    rows: dict[str, str] = {}
    for match in re.finditer(
        r"^\|\s*`(\d{3})`\s*\|\s*`([^`]+)`\s*\|", section, flags=re.M
    ):
        rows[match.group(1)] = match.group(2)
    return rows


def _legacy_outside_compatibility(text: str) -> list[str]:
    """Return legacy pipeline identifiers used outside allowed documentation zones."""
    compat_start = text.find(COMPATIBILITY_HEADING)
    migration_015_start = text.find(MIGRATION_015_HEADING)
    migration_015_end = text.find(MIGRATION_016_HEADING)
    if migration_015_end < 0:
        migration_015_end = text.find("### Concurrent startup")

    exclude_spans: list[tuple[int, int]] = []
    if compat_start >= 0:
        # Compatibility subsection ends at the next ### table heading.
        compat_end = text.find("\n### `", compat_start + 1)
        if compat_end < 0:
            compat_end = len(text)
        exclude_spans.append((compat_start, compat_end))
    if migration_015_start >= 0 and migration_015_end > migration_015_start:
        exclude_spans.append((migration_015_start, migration_015_end))

    operational_parts: list[str] = []
    last = 0
    for start, end in sorted(exclude_spans):
        if start > last:
            operational_parts.append(text[last:start])
        last = max(last, end)
    if last < len(text):
        operational_parts.append(text[last:])
    operational = "".join(operational_parts)

    offenders: list[str] = []
    patterns = (
        (r"(?<![\w_])owner(?![\w_])", "owner"),
        (r"(?<![\w_])expected_value(?![\w_])", "expected_value"),
        (r"\bstage_reason\b", "stage_reason"),
        (r"\bcompany_stage_history\b", "company_stage_history"),
        (r"\bidx_companies_owner\b", "idx_companies_owner"),
    )
    for pattern, label in patterns:
        if re.search(pattern, operational):
            offenders.append(label)

    if compat_start < 0:
        offenders.append("missing compatibility mention: section")
        return offenders

    compat = text[compat_start : exclude_spans[0][1]]
    for label in LEGACY_PIPELINE_COLUMNS:
        if label not in compat:
            offenders.append(f"missing compatibility mention: {label}")
    if "company_stage_history" not in compat:
        offenders.append("missing compatibility mention: company_stage_history")
    return offenders


def check_crm_schema_docs(
    *,
    schema_path: Path = CRM_SCHEMA_PATH,
    root: Path = ROOT,
) -> CheckResult:
    errors: list[str] = []
    if not schema_path.is_file():
        return CheckResult(False, (f"missing {schema_path}",))

    text = schema_path.read_text(encoding="utf-8")
    migrations = load_definitions()
    expected_ledger = {
        version: name
        for version, name in migrations
        if version <= LEDGER_MAX_VERSION
    }
    expected_versions = [v for v, _ in migrations if v <= LEDGER_MAX_VERSION]
    if expected_versions != sorted(expected_versions, key=int):
        errors.append("migration registry is not ordered through 016")

    ledger = _migration_ledger_rows(text)
    for version in expected_versions:
        if version not in ledger:
            errors.append(f"migration ledger missing version {version}")
        elif ledger[version] != expected_ledger[version]:
            errors.append(
                f"migration {version} name mismatch: doc={ledger[version]!r} "
                f"expected={expected_ledger[version]!r}"
            )
    for version in ledger:
        if int(version) <= int(LEDGER_MAX_VERSION) and version not in expected_ledger:
            errors.append(f"migration ledger documents unknown version {version}")

    companies_section = _section(
        text,
        "### `companies`",
        stop_at=COMPATIBILITY_HEADING,
    )
    if not companies_section.strip():
        errors.append("companies table section not found")
    else:
        documented = _table_column_names(companies_section)
        missing = CANONICAL_PIPELINE_COLUMNS - documented
        if missing:
            errors.append(
                "companies table missing canonical pipeline columns: "
                + ", ".join(sorted(missing))
            )
        legacy_in_canonical = LEGACY_PIPELINE_COLUMNS & documented
        if legacy_in_canonical:
            errors.append(
                "legacy pipeline columns documented as canonical: "
                + ", ".join(sorted(legacy_in_canonical))
            )

    compat_section = _section(text, COMPATIBILITY_HEADING, stop_at="### `contacts`")
    if not compat_section.strip():
        errors.append("compatibility artifacts subsection missing")
    else:
        compat_cols = _table_column_names(compat_section)
        missing_legacy = LEGACY_PIPELINE_COLUMNS - compat_cols
        if missing_legacy:
            errors.append(
                "compatibility section missing legacy columns: "
                + ", ".join(sorted(missing_legacy))
            )

    briefs_section = _section(
        text,
        "### `project_briefs`",
        stop_at="### `companies`",
    )
    if not briefs_section.strip():
        errors.append("project_briefs table section not found")
    else:
        brief_cols = _table_column_names(briefs_section)
        missing_payment = PAYMENT_DETAIL_COLUMNS - brief_cols
        if missing_payment:
            errors.append(
                "project_briefs section missing payment columns: "
                + ", ".join(sorted(missing_payment))
            )

    for label in _legacy_outside_compatibility(text):
        errors.append(f"legacy identifier {label!r} outside compatibility section")

    required_phrases = (
        "reconcile_acquisition_pipeline_schema",
        "project_brief_payment_details",
        "FROZEN_MIGRATION_DIGESTS",
        "expected_value_cents",
        "pipeline_loss_reason",
        "pipeline_nurture_reason",
    )
    for phrase in required_phrases:
        if phrase not in text:
            errors.append(f"CRM_SCHEMA.md missing required phrase: {phrase}")

    drift_script = root / "scripts" / "check_crm_schema_docs.py"
    if not drift_script.is_file():
        errors.append("scripts/check_crm_schema_docs.py missing")

    return CheckResult(not errors, tuple(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        type=Path,
        default=CRM_SCHEMA_PATH,
        help="Path to CRM_SCHEMA.md",
    )
    args = parser.parse_args(argv)
    result = check_crm_schema_docs(schema_path=args.schema)
    if result.ok:
        print("CRM schema documentation check passed.")
        return 0
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
