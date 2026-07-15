"""Acquisition pipeline stages, transitions, and activity types."""

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

MAIN_PATH: Final[tuple[str, ...]] = (
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
)

TERMINAL_STAGES: Final[frozenset[str]] = frozenset({"won", "lost"})
REASON_REQUIRED_STAGES: Final[frozenset[str]] = frozenset({"lost", "nurture"})
ACTIVE_STAGES: Final[frozenset[str]] = frozenset(set(PIPELINE_STAGES) - TERMINAL_STAGES - {"nurture"})

PIPELINE_ACTIVITY_TYPES: Final[tuple[str, ...]] = (
    "note",
    "outreach",
    "reply",
    "meeting",
    "proposal",
    "payment",
    "task_completion",
)

LEGACY_ACTIVITY_TYPES: Final[tuple[str, ...]] = (
    "email",
    "call",
    "status_change",
)

ALL_ACTIVITY_TYPES: Final[tuple[str, ...]] = PIPELINE_ACTIVITY_TYPES + LEGACY_ACTIVITY_TYPES


class PipelineError(Exception):
    """Base pipeline validation error."""


class InvalidStageError(PipelineError):
    """Raised when a stage name is not in the supported pipeline."""


class InvalidTransitionError(PipelineError):
    """Raised when a stage transition is not allowed."""


class ConfirmRequiredError(PipelineError):
    """Raised when a non-adjacent transition needs explicit confirmation."""


class ReasonRequiredError(PipelineError):
    """Raised when moving to lost or nurture without a reason."""


def _main_path_index(stage: str) -> int:
    return MAIN_PATH.index(stage)


def _on_main_path(stage: str) -> bool:
    return stage in MAIN_PATH


def validate_stage(stage: str) -> None:
    if stage not in PIPELINE_STAGES:
        raise InvalidStageError(f"Unsupported pipeline stage: {stage}")


def validate_activity_type(activity_type: str) -> None:
    if activity_type not in ALL_ACTIVITY_TYPES:
        raise InvalidStageError(f"Unsupported activity type: {activity_type}")


def validate_transition(
    from_stage: str,
    to_stage: str,
    *,
    confirm: bool = False,
    reason: str | None = None,
) -> None:
    """Validate a pipeline stage transition.

    Adjacent forward/backward moves on the main path and exits to lost/nurture
    are allowed without confirmation. Skips, reopening from terminal states, and
    multi-step backward moves require ``confirm=True``. Lost and nurture require
    a non-empty ``reason``.
    """
    validate_stage(from_stage)
    validate_stage(to_stage)

    if from_stage == to_stage:
        raise InvalidTransitionError("Company is already in this stage")

    if to_stage in REASON_REQUIRED_STAGES and not (reason and reason.strip()):
        raise ReasonRequiredError(f"Reason is required when moving to {to_stage}")

    if _allowed_without_confirm(from_stage, to_stage):
        return

    if _allowed_with_confirm(from_stage, to_stage):
        if not confirm:
            raise ConfirmRequiredError(
                f"Transition from {from_stage} to {to_stage} requires confirmation"
            )
        return

    raise InvalidTransitionError(f"Transition from {from_stage} to {to_stage} is not allowed")


def _allowed_without_confirm(from_stage: str, to_stage: str) -> bool:
    if to_stage in REASON_REQUIRED_STAGES and from_stage not in REASON_REQUIRED_STAGES:
        return True

    if _on_main_path(from_stage) and _on_main_path(to_stage):
        from_idx = _main_path_index(from_stage)
        to_idx = _main_path_index(to_stage)
        if to_idx == from_idx + 1 or to_idx == from_idx - 1:
            return True

    return False


def _allowed_with_confirm(from_stage: str, to_stage: str) -> bool:
    if from_stage in TERMINAL_STAGES or from_stage == "nurture":
        if to_stage not in TERMINAL_STAGES and to_stage != "nurture":
            return True

    if _on_main_path(from_stage) and _on_main_path(to_stage):
        from_idx = _main_path_index(from_stage)
        to_idx = _main_path_index(to_stage)
        if to_idx > from_idx + 1:
            return True
        if to_idx < from_idx - 1:
            return True

    if from_stage in REASON_REQUIRED_STAGES and _on_main_path(to_stage):
        return True

    if from_stage in REASON_REQUIRED_STAGES and to_stage in REASON_REQUIRED_STAGES:
        return from_stage != to_stage

    return False
