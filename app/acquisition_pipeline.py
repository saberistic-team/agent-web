"""Acquisition pipeline stages, activities, and transition rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.pipeline_registry import (
    PIPELINE_STAGE_ORDER,
    TERMINAL_STAGES,
    pipeline_stage_display_label,
    pipeline_stages_ordered,
)

PIPELINE_STAGES: dict[str, str] = pipeline_stages_ordered()
STAGE_ORDER: tuple[str, ...] = PIPELINE_STAGE_ORDER

PIPELINE_ACTIVITY_TYPES: dict[str, str] = {
    "note": "Note",
    "outreach": "Outreach",
    "reply": "Reply",
    "meeting": "Meeting",
    "proposal": "Proposal",
    "payment": "Payment",
    "task_completion": "Task completion",
}

# Legacy activity types kept for existing rows.
LEGACY_ACTIVITY_TYPES = frozenset({"email", "call", "status_change"})


class PipelineTransitionError(ValueError):
    """Raised when a stage change is not allowed."""


def pipeline_stage_label(stage: str | None) -> str:
    if not stage:
        return "—"
    return pipeline_stage_display_label(stage)


def validate_pipeline_stage(stage: str) -> str:
    if stage not in PIPELINE_STAGES:
        allowed = ", ".join(PIPELINE_STAGES)
        raise ValueError(f"Invalid pipeline stage. Allowed: {allowed}")
    return stage


def validate_pipeline_activity_type(activity_type: str) -> str:
    if activity_type not in PIPELINE_ACTIVITY_TYPES:
        allowed = ", ".join(PIPELINE_ACTIVITY_TYPES)
        raise ValueError(f"Invalid activity type. Allowed: {allowed}")
    return activity_type


def _stage_index(stage: str) -> int | None:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return None


def assess_stage_transition(
    from_stage: str | None,
    to_stage: str,
    *,
    confirm: bool = False,
    loss_reason: str | None = None,
    nurture_reason: str | None = None,
) -> None:
    """Validate a pipeline stage transition; raise PipelineTransitionError if blocked."""
    validate_pipeline_stage(to_stage)

    if from_stage == to_stage:
        return

    if from_stage is not None:
        validate_pipeline_stage(from_stage)

    if to_stage == "lost":
        if not loss_reason and not confirm:
            raise PipelineTransitionError(
                "Moving to Lost requires a loss reason or explicit confirmation."
            )
        return

    if to_stage == "nurture":
        if not nurture_reason and not confirm:
            raise PipelineTransitionError(
                "Moving to Nurture requires a nurture reason or explicit confirmation."
            )
        return

    if from_stage is None:
        return

    if from_stage in TERMINAL_STAGES and to_stage not in TERMINAL_STAGES:
        if not confirm:
            raise PipelineTransitionError(
                f"Reopening from {pipeline_stage_label(from_stage)} requires explicit confirmation."
            )
        return

    from_index = _stage_index(from_stage)
    to_index = _stage_index(to_stage)

    if from_index is not None and to_index is not None:
        if to_index == from_index + 1:
            return
        if to_index > from_index + 1 and not confirm:
            raise PipelineTransitionError(
                "Skipping pipeline stages requires explicit confirmation."
            )
        if to_index < from_index and not confirm:
            raise PipelineTransitionError(
                "Moving backward in the pipeline requires explicit confirmation."
            )
        return

    if to_stage == "won" and from_stage != "larger_engagement" and not confirm:
        raise PipelineTransitionError(
            "Marking Won before Larger engagement requires explicit confirmation."
        )

    if not confirm:
        raise PipelineTransitionError(
            "This stage change requires explicit confirmation."
        )


class PipelineStageChange(BaseModel):
    to_stage: str
    confirm: bool = False
    loss_reason: str | None = Field(default=None, max_length=2000)
    nurture_reason: str | None = Field(default=None, max_length=2000)

    @field_validator("to_stage")
    @classmethod
    def _validate_stage(cls, value: str) -> str:
        return validate_pipeline_stage(value.strip())

    @model_validator(mode="after")
    def _normalize_reasons(self) -> PipelineStageChange:
        self.loss_reason = self.loss_reason.strip() if self.loss_reason else None
        self.nurture_reason = self.nurture_reason.strip() if self.nurture_reason else None
        return self


class PipelineNextActionUpdate(BaseModel):
    next_action: str | None = Field(default=None, max_length=2000)
    next_action_due_at: datetime | None = None
    pipeline_owner: str | None = Field(default=None, max_length=200)
    expected_value_cents: int | None = Field(default=None, ge=0)

    @field_validator("next_action", "pipeline_owner", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PipelineActivityCreate(BaseModel):
    activity_type: str
    summary: str = Field(min_length=1, max_length=5000)
    contact_id: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("activity_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        return validate_pipeline_activity_type(value.strip())

    @field_validator("summary")
    @classmethod
    def _strip_summary(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Summary is required.")
        return stripped


def pipeline_summary(company: dict[str, Any]) -> dict[str, Any]:
    """Compact pipeline snapshot for audit events."""
    return {
        "pipeline_stage": company.get("pipeline_stage"),
        "next_action": company.get("next_action"),
        "next_action_due_at": (
            company["next_action_due_at"].isoformat()
            if company.get("next_action_due_at") is not None
            else None
        ),
        "pipeline_owner": company.get("pipeline_owner"),
        "expected_value_cents": company.get("expected_value_cents"),
        "pipeline_loss_reason": company.get("pipeline_loss_reason"),
        "pipeline_nurture_reason": company.get("pipeline_nurture_reason"),
    }
