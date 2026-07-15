"""Unit tests for acquisition pipeline stage transitions."""

from __future__ import annotations

import pytest

from app.pipeline import (
    ConfirmRequiredError,
    InvalidTransitionError,
    PIPELINE_STAGES,
    ReasonRequiredError,
    validate_transition,
)


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_supports_all_required_stages() -> None:
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


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    ("from_stage", "to_stage"),
    [
        ("researching", "qualified"),
        ("qualified", "ready_for_outreach"),
        ("ready_for_outreach", "contacted"),
        ("contacted", "replied"),
        ("replied", "discovery_scheduled"),
        ("discovery_scheduled", "diagnostic_proposed"),
        ("diagnostic_proposed", "diagnostic_paid"),
        ("diagnostic_paid", "larger_engagement"),
        ("larger_engagement", "won"),
        ("qualified", "researching"),
        ("contacted", "ready_for_outreach"),
    ],
)
def test_adjacent_transitions_allowed_without_confirm(from_stage: str, to_stage: str) -> None:
    validate_transition(from_stage, to_stage)


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    ("from_stage", "to_stage"),
    [
        ("researching", "lost"),
        ("qualified", "nurture"),
        ("contacted", "lost"),
        ("diagnostic_paid", "nurture"),
    ],
)
def test_side_exit_requires_reason(from_stage: str, to_stage: str) -> None:
    with pytest.raises(ReasonRequiredError):
        validate_transition(from_stage, to_stage)
    validate_transition(from_stage, to_stage, reason="Not a fit right now")


@pytest.mark.unit
@pytest.mark.integration
def test_skip_forward_requires_confirm() -> None:
    with pytest.raises(ConfirmRequiredError):
        validate_transition("researching", "contacted")
    validate_transition("researching", "contacted", confirm=True)


@pytest.mark.unit
@pytest.mark.integration
def test_reopen_from_lost_requires_confirm() -> None:
    with pytest.raises(ConfirmRequiredError):
        validate_transition("lost", "researching", reason="Re-engaged")
    validate_transition("lost", "researching", reason="Re-engaged", confirm=True)


@pytest.mark.unit
@pytest.mark.integration
def test_same_stage_is_rejected() -> None:
    with pytest.raises(InvalidTransitionError, match="already"):
        validate_transition("qualified", "qualified")


@pytest.mark.unit
@pytest.mark.integration
def test_multi_step_backward_requires_confirm() -> None:
    with pytest.raises(ConfirmRequiredError):
        validate_transition("diagnostic_paid", "qualified")
    validate_transition("diagnostic_paid", "qualified", confirm=True)


@pytest.mark.unit
@pytest.mark.integration
def test_validate_stage_and_activity_type_reject_unknown_values() -> None:
    from app.pipeline import InvalidStageError, validate_activity_type, validate_stage

    with pytest.raises(InvalidStageError):
        validate_stage("invalid")
    with pytest.raises(InvalidStageError):
        validate_activity_type("fax")


@pytest.mark.unit
@pytest.mark.integration
def test_reopen_from_lost_to_main_path_with_confirm() -> None:
    validate_transition("lost", "qualified", reason="Back in play", confirm=True)


@pytest.mark.unit
@pytest.mark.integration
def test_transition_between_lost_and_nurture_requires_confirm() -> None:
    with pytest.raises(ConfirmRequiredError):
        validate_transition("lost", "nurture", reason="Long-term follow-up")
    validate_transition("lost", "nurture", reason="Long-term follow-up", confirm=True)
