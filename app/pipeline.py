"""Acquisition pipeline stages and brief-to-pipeline mapping."""

from __future__ import annotations

from typing import Final

from app.pipeline_registry import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    PIPELINE_STAGE_ORDER,
    pipeline_stage_display_label,
)

PIPELINE_STAGES: Final[tuple[str, ...]] = PIPELINE_STAGE_ORDER


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
    return pipeline_stage_display_label(stage)
