"""Brief-to-pipeline mapping and validation (imports canonical stage registry)."""

from __future__ import annotations

from typing import Final

from app.pipeline_stages import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    PIPELINE_STAGE_LABELS,
    PIPELINE_STAGE_ORDER,
    InvalidStageError,
    PipelineError,
    initial_pipeline_stage_for_brief_status,
    pipeline_stage_label,
    resolve_initial_pipeline_stage_for_brief_status,
    validate_stage,
)

PIPELINE_STAGES: Final[tuple[str, ...]] = PIPELINE_STAGE_ORDER

__all__ = [
    "BRIEF_STATUS_INITIAL_PIPELINE_STAGE",
    "PIPELINE_STAGE_LABELS",
    "PIPELINE_STAGES",
    "InvalidStageError",
    "PipelineError",
    "initial_pipeline_stage_for_brief_status",
    "pipeline_stage_label",
    "resolve_initial_pipeline_stage_for_brief_status",
    "validate_stage",
]
