"""Acquisition pipeline stages and brief-to-pipeline mapping."""

from __future__ import annotations

from typing import Final

PIPELINE_STAGES: Final[tuple[str, ...]] = (
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

# Initial pipeline stage when converting a project brief. Payment state comes from
# the brief row only — never from operator form input.
BRIEF_STATUS_INITIAL_PIPELINE_STAGE: Final[dict[str, str]] = {
    "paid": "diagnostic_paid",
    "pending_payment": "qualified",
    "abandoned": "qualified",
}


class PipelineError(Exception):
    """Base pipeline validation error."""


class InvalidStageError(PipelineError):
    """Raised when a stage name is not in the supported pipeline."""


def validate_stage(stage: str) -> None:
    if stage not in PIPELINE_STAGES:
        raise InvalidStageError(f"Unsupported pipeline stage: {stage}")


def initial_pipeline_stage_for_brief_status(brief_status: str) -> str:
    """Map Stripe-derived brief payment status to an initial pipeline stage."""
    stage = BRIEF_STATUS_INITIAL_PIPELINE_STAGE.get(brief_status)
    if stage is None:
        raise InvalidStageError(f"Unsupported brief status for pipeline conversion: {brief_status}")
    validate_stage(stage)
    return stage


def pipeline_stage_label(stage: str) -> str:
    return PIPELINE_STAGE_LABELS.get(stage, stage.replace("_", " ").title())
