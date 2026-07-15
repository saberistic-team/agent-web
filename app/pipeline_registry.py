"""Canonical acquisition pipeline stage registry.

Single source of truth for stage keys, display labels, ordering, terminal/side-exit
semantics, and brief-status mappings. Brief conversion, pipeline validation, transition
rules, admin UI, preview fixtures, and database constraints must derive from here.
"""

from __future__ import annotations

import re
from typing import Final

PIPELINE_STAGE_ORDER: Final[tuple[str, ...]] = (
    "researching",
    "qualified",
    "ready_for_outreach",
    "contacted",
    "replied",
    "discovery_scheduled",
    "diagnostic_proposed",
    "diagnostic_paid",
    "larger_engagement",
    "won",
    "lost",
    "nurture",
)

PIPELINE_STAGE_LABELS: Final[dict[str, str]] = {
    "researching": "Researching",
    "qualified": "Qualified",
    "ready_for_outreach": "Ready for outreach",
    "contacted": "Contacted",
    "replied": "Replied",
    "discovery_scheduled": "Discovery scheduled",
    "diagnostic_proposed": "Diagnostic proposed",
    "diagnostic_paid": "Diagnostic paid",
    "larger_engagement": "Larger engagement",
    "won": "Won",
    "lost": "Lost",
    "nurture": "Nurture",
}

DEFAULT_PIPELINE_STAGE: Final[str] = "researching"

TERMINAL_STAGES: Final[frozenset[str]] = frozenset({"won", "lost"})
SIDE_EXIT_STAGES: Final[frozenset[str]] = frozenset({"lost", "nurture"})

BRIEF_STATUS_INITIAL_PIPELINE_STAGE: Final[dict[str, str]] = {
    "paid": "diagnostic_paid",
    "pending_payment": "qualified",
    "abandoned": "qualified",
}


def pipeline_stages_ordered() -> dict[str, str]:
    """Stage key → display label in canonical pipeline order."""
    return {key: PIPELINE_STAGE_LABELS[key] for key in PIPELINE_STAGE_ORDER}


def pipeline_stage_display_label(stage: str) -> str:
    """Return the canonical label for a stage, with a title-case fallback."""
    return PIPELINE_STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def pipeline_stage_check_constraint_literals() -> tuple[str, ...]:
    """Ordered stage keys used in ``companies.pipeline_stage`` CHECK constraints."""
    return PIPELINE_STAGE_ORDER


def pipeline_stage_check_constraint_sql() -> str:
    """SQL literal list for ``CHECK (pipeline_stage IN (...))``."""
    return ", ".join(f"'{stage}'" for stage in PIPELINE_STAGE_ORDER)


def extract_pipeline_stage_check_values(migration_sql: str) -> tuple[str, ...]:
    """Parse stage literals from a migration CHECK constraint for contract tests."""
    match = re.search(
        r"companies_pipeline_stage_check\s*\n\s*CHECK \(pipeline_stage IN \(([^)]+)\)\)",
        migration_sql,
        re.DOTALL,
    )
    if match is None:
        raise ValueError("companies_pipeline_stage_check constraint not found")
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


def _validate_registry() -> None:
    if len(PIPELINE_STAGE_ORDER) != len(set(PIPELINE_STAGE_ORDER)):
        raise RuntimeError("Duplicate pipeline stage keys in PIPELINE_STAGE_ORDER")

    label_keys = set(PIPELINE_STAGE_LABELS)
    order_keys = set(PIPELINE_STAGE_ORDER)
    if label_keys != order_keys:
        missing = order_keys - label_keys
        extra = label_keys - order_keys
        raise RuntimeError(
            "PIPELINE_STAGE_LABELS keys must match PIPELINE_STAGE_ORDER "
            f"(missing labels: {sorted(missing)!r}, extra labels: {sorted(extra)!r})"
        )

    for key in PIPELINE_STAGE_ORDER:
        label = PIPELINE_STAGE_LABELS[key]
        if not label or not label.strip():
            raise RuntimeError(f"Pipeline stage {key!r} has an empty display label")

    for status, stage in BRIEF_STATUS_INITIAL_PIPELINE_STAGE.items():
        if stage not in order_keys:
            raise RuntimeError(
                f"Brief status {status!r} maps to unknown pipeline stage {stage!r}"
            )


_validate_registry()
