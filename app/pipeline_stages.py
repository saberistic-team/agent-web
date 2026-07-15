"""Canonical acquisition pipeline stage registry.

Single source of truth for stage keys, display labels, ordering, terminal and
side-exit semantics, brief-status mappings, and database check-constraint values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = (
    "BRIEF_STATUS_INITIAL_PIPELINE_STAGE",
    "InvalidStageError",
    "PIPELINE_STAGE_KEYS",
    "PIPELINE_STAGES",
    "PipelineError",
    "SIDE_EXIT_STAGES",
    "STAGE_ORDER",
    "TERMINAL_STAGES",
    "initial_pipeline_stage_for_brief_status",
    "is_valid_pipeline_stage",
    "parse_pipeline_stage_check_constraint",
    "pipeline_stage_db_check_values",
    "pipeline_stage_label",
    "validate_pipeline_stage",
    "validate_stage",
)


@dataclass(frozen=True, slots=True)
class _PipelineStageDef:
    key: str
    label: str
    in_linear_order: bool = True
    terminal: bool = False
    side_exit: bool = False


_PIPELINE_STAGE_DEFS: Final[tuple[_PipelineStageDef, ...]] = (
    _PipelineStageDef("researching", "Researching"),
    _PipelineStageDef("qualified", "Qualified"),
    _PipelineStageDef("ready_for_outreach", "Ready for outreach"),
    _PipelineStageDef("contacted", "Contacted"),
    _PipelineStageDef("replied", "Replied"),
    _PipelineStageDef("discovery_scheduled", "Discovery scheduled"),
    _PipelineStageDef("diagnostic_proposed", "Diagnostic proposed"),
    _PipelineStageDef("diagnostic_paid", "Diagnostic paid"),
    _PipelineStageDef("larger_engagement", "Larger engagement"),
    _PipelineStageDef("won", "Won", terminal=True),
    _PipelineStageDef("lost", "Lost", in_linear_order=False, terminal=True, side_exit=True),
    _PipelineStageDef("nurture", "Nurture", in_linear_order=False, side_exit=True),
)

PIPELINE_STAGE_KEYS: Final[tuple[str, ...]] = tuple(stage.key for stage in _PIPELINE_STAGE_DEFS)

PIPELINE_STAGES: Final[dict[str, str]] = {
    stage.key: stage.label for stage in _PIPELINE_STAGE_DEFS
}

STAGE_ORDER: Final[tuple[str, ...]] = tuple(
    stage.key for stage in _PIPELINE_STAGE_DEFS if stage.in_linear_order
)

TERMINAL_STAGES: Final[frozenset[str]] = frozenset(
    stage.key for stage in _PIPELINE_STAGE_DEFS if stage.terminal
)

SIDE_EXIT_STAGES: Final[frozenset[str]] = frozenset(
    stage.key for stage in _PIPELINE_STAGE_DEFS if stage.side_exit
)

BRIEF_STATUS_INITIAL_PIPELINE_STAGE: Final[dict[str, str]] = {
    "paid": "diagnostic_paid",
    "pending_payment": "qualified",
    "abandoned": "qualified",
}

_CHECK_CONSTRAINT_STAGE_RE = re.compile(
    r"companies_pipeline_stage_check[\s\S]*?CHECK\s*\(\s*pipeline_stage\s+IN\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_CHECK_VALUE_RE = re.compile(r"'([^']+)'")


class PipelineError(Exception):
    """Base pipeline validation error."""


class InvalidStageError(PipelineError):
    """Raised when a stage name is not in the supported pipeline."""


def pipeline_stage_label(stage: str | None) -> str:
    if not stage:
        return "—"
    return PIPELINE_STAGES.get(stage, stage.replace("_", " ").title())


def is_valid_pipeline_stage(stage: str) -> bool:
    return stage in PIPELINE_STAGES


def validate_pipeline_stage(stage: str) -> str:
    if stage not in PIPELINE_STAGES:
        allowed = ", ".join(PIPELINE_STAGES)
        raise ValueError(f"Invalid pipeline stage. Allowed: {allowed}")
    return stage


def validate_stage(stage: str) -> None:
    if stage not in PIPELINE_STAGES:
        raise InvalidStageError(f"Unsupported pipeline stage: {stage}")


def initial_pipeline_stage_for_brief_status(brief_status: str) -> str:
    """Map Stripe-derived brief payment status to an initial pipeline stage."""
    mapped = BRIEF_STATUS_INITIAL_PIPELINE_STAGE.get(brief_status)
    if mapped is None:
        raise InvalidStageError(
            f"Unsupported brief status for pipeline conversion: {brief_status}"
        )
    validate_stage(mapped)
    return mapped


def pipeline_stage_db_check_values() -> tuple[str, ...]:
    """Stage keys allowed by ``companies_pipeline_stage_check``."""
    return PIPELINE_STAGE_KEYS


def parse_pipeline_stage_check_constraint(sql: str) -> frozenset[str]:
    """Extract allowed stage keys from migration SQL for schema-contract tests."""
    match = _CHECK_CONSTRAINT_STAGE_RE.search(sql)
    if match is None:
        raise ValueError("companies_pipeline_stage_check constraint not found in SQL")
    return frozenset(_CHECK_VALUE_RE.findall(match.group(1)))
