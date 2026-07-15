"""Canonical acquisition pipeline stage registry.

Single source of truth for stage keys, display labels, ordering,
terminal/side-exit semantics, and brief-status → initial stage mappings.
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

# Ordered registry for UI/forms (insertion order matches PIPELINE_STAGE_ORDER).
PIPELINE_STAGES: Final[dict[str, str]] = {
    stage: PIPELINE_STAGE_LABELS[stage] for stage in PIPELINE_STAGE_ORDER
}

STAGE_ORDER: Final[tuple[str, ...]] = PIPELINE_STAGE_ORDER

TERMINAL_STAGES: Final[frozenset[str]] = frozenset({"won", "lost"})
SIDE_EXIT_STAGES: Final[frozenset[str]] = frozenset({"lost", "nurture"})

# Initial pipeline stage when converting a project brief. Payment state comes from
# the brief row only — never from operator form input.
BRIEF_STATUS_INITIAL_PIPELINE_STAGE: Final[dict[str, str]] = {
    "paid": "diagnostic_paid",
    "pending_payment": "qualified",
    "abandoned": "qualified",
}

_EMPTY_STAGE_LABEL = "—"
_PIPELINE_STAGE_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*pipeline_stage\s+IN\s*\((.*?)\)\s*\)",
    re.DOTALL | re.IGNORECASE,
)


def pipeline_stage_label(stage: str | None) -> str:
    if not stage:
        return _EMPTY_STAGE_LABEL
    return PIPELINE_STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def is_valid_pipeline_stage(stage: str) -> bool:
    return stage in PIPELINE_STAGE_LABELS


def resolve_initial_pipeline_stage_for_brief_status(brief_status: str) -> str | None:
    """Return the mapped initial stage for a brief status, or None when unknown."""
    return BRIEF_STATUS_INITIAL_PIPELINE_STAGE.get(brief_status)


def pipeline_stage_check_sql_fragment() -> str:
    """SQL fragment for companies.pipeline_stage CHECK constraint values."""
    quoted = ", ".join(f"'{stage}'" for stage in PIPELINE_STAGE_ORDER)
    return (
        "CHECK (pipeline_stage IN (\n"
        f"        {quoted}\n"
        "    ))"
    )


def extract_pipeline_stage_check_values(sql: str) -> frozenset[str]:
    """Parse stage keys from a migration CHECK (pipeline_stage IN (...)) clause."""
    match = _PIPELINE_STAGE_CHECK_RE.search(sql)
    if match is None:
        raise ValueError("pipeline_stage CHECK constraint not found in SQL")
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))

class PipelineError(Exception):
    """Base pipeline validation error."""


class InvalidStageError(PipelineError):
    """Raised when a stage name is not in the supported pipeline."""


def validate_stage(stage: str) -> None:
    if stage not in PIPELINE_STAGE_ORDER:
        raise InvalidStageError(f"Unsupported pipeline stage: {stage}")


def initial_pipeline_stage_for_brief_status(brief_status: str) -> str:
    """Map Stripe-derived brief payment status to an initial pipeline stage."""
    stage = resolve_initial_pipeline_stage_for_brief_status(brief_status)
    if stage is None:
        raise InvalidStageError(
            f"Unsupported brief status for pipeline conversion: {brief_status}"
        )
    validate_stage(stage)
    return stage

