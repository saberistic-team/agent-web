"""Brief-to-pipeline mapping helpers (re-exported from the canonical registry)."""

from __future__ import annotations

from app.pipeline_stages import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    InvalidStageError,
    PIPELINE_STAGE_KEYS,
    PIPELINE_STAGES,
    PipelineError,
    initial_pipeline_stage_for_brief_status,
    pipeline_stage_label,
    validate_stage,
)

# Backward-compatible aliases for callers that imported tuple/dict names from here.
PIPELINE_STAGES_TUPLE = PIPELINE_STAGE_KEYS
PIPELINE_STAGE_LABELS = PIPELINE_STAGES

__all__ = (
    "BRIEF_STATUS_INITIAL_PIPELINE_STAGE",
    "InvalidStageError",
    "PIPELINE_STAGE_KEYS",
    "PIPELINE_STAGE_LABELS",
    "PIPELINE_STAGES",
    "PIPELINE_STAGES_TUPLE",
    "PipelineError",
    "initial_pipeline_stage_for_brief_status",
    "pipeline_stage_label",
    "validate_stage",
)
