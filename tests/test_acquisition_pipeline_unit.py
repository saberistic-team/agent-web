"""Unit tests for acquisition pipeline domain rules."""

from __future__ import annotations

import pytest

from app.acquisition_pipeline import (
    PIPELINE_STAGES,
    PipelineStageChange,
    PipelineTransitionError,
    assess_stage_transition,
    validate_pipeline_activity_type,
    validate_pipeline_stage,
)

pytestmark = [pytest.mark.unit, pytest.mark.integration]


def test_pipeline_stage_registry_covers_acceptance_stages() -> None:
    expected = {
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
    }
    assert set(PIPELINE_STAGES) == expected


def test_adjacent_forward_transition_allowed() -> None:
    assess_stage_transition("researching", "qualified")


def test_skip_forward_requires_confirm() -> None:
    with pytest.raises(PipelineTransitionError, match="Skipping"):
        assess_stage_transition("researching", "contacted")
    assess_stage_transition("researching", "contacted", confirm=True)


def test_backward_transition_requires_confirm() -> None:
    with pytest.raises(PipelineTransitionError, match="backward"):
        assess_stage_transition("contacted", "qualified")
    assess_stage_transition("contacted", "qualified", confirm=True)


def test_lost_requires_reason_or_confirm() -> None:
    with pytest.raises(PipelineTransitionError, match="Lost"):
        assess_stage_transition("qualified", "lost")
    assess_stage_transition("qualified", "lost", loss_reason="No budget")
    assess_stage_transition("qualified", "lost", confirm=True)


def test_nurture_requires_reason_or_confirm() -> None:
    with pytest.raises(PipelineTransitionError, match="Nurture"):
        assess_stage_transition("qualified", "nurture")
    assess_stage_transition("qualified", "nurture", nurture_reason="Timing")


def test_reopen_from_won_requires_confirm() -> None:
    with pytest.raises(PipelineTransitionError, match="Reopening"):
        assess_stage_transition("won", "qualified")
    assess_stage_transition("won", "qualified", confirm=True)


def test_initial_assignment_from_none() -> None:
    assess_stage_transition(None, "researching")


def test_same_stage_is_noop() -> None:
    assess_stage_transition("qualified", "qualified")


def test_activity_types_include_required_values() -> None:
    for activity_type in (
        "note",
        "outreach",
        "reply",
        "meeting",
        "proposal",
        "payment",
        "task_completion",
    ):
        assert validate_pipeline_activity_type(activity_type) == activity_type


def test_invalid_activity_type_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid activity type"):
        validate_pipeline_activity_type("tweet")


def test_invalid_stage_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid pipeline stage"):
        validate_pipeline_stage("proposal")


def test_pipeline_stage_label_and_summary() -> None:
    from app.acquisition_pipeline import pipeline_stage_label, pipeline_summary

    assert pipeline_stage_label("qualified") == "Qualified"
    assert pipeline_stage_label("") == "—"
    assert pipeline_stage_label("custom_stage") == "Custom Stage"
    assert pipeline_summary({"pipeline_stage": "qualified"})["pipeline_stage"] == "qualified"


def test_won_from_larger_engagement_allowed() -> None:
    assess_stage_transition("larger_engagement", "won")


def test_won_skip_with_confirm_allowed() -> None:
    assess_stage_transition("researching", "won", confirm=True)


def test_pipeline_stage_change_model_normalizes() -> None:
    change = PipelineStageChange(to_stage=" qualified ", loss_reason="  budget ")
    assert change.to_stage == "qualified"
    assert change.loss_reason == "budget"


def test_pipeline_models_blank_and_summary_validation() -> None:
    from app.acquisition_pipeline import PipelineActivityCreate, PipelineNextActionUpdate
    from pydantic import ValidationError

    update = PipelineNextActionUpdate(next_action="  ", pipeline_owner="  ")
    assert update.next_action is None
    assert update.pipeline_owner is None

    activity = PipelineActivityCreate(activity_type="note", summary="  hello  ")
    assert activity.summary == "hello"

    with pytest.raises(ValidationError):
        PipelineActivityCreate(activity_type="note", summary="   ")
