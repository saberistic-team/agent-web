"""Unit tests for the canonical pipeline stage registry."""

from __future__ import annotations

import pytest

from app.acquisition_pipeline import PIPELINE_STAGES as ACQUISITION_PIPELINE_STAGES
from app.acquisition_pipeline import STAGE_ORDER
from app.migrations.definitions import MIGRATIONS
from app.pipeline import PIPELINE_STAGES as PIPELINE_STAGE_KEYS
from app.pipeline_registry import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    DEFAULT_PIPELINE_STAGE,
    PIPELINE_STAGE_LABELS,
    PIPELINE_STAGE_ORDER,
    SIDE_EXIT_STAGES,
    TERMINAL_STAGES,
    extract_pipeline_stage_check_values,
    pipeline_stage_check_constraint_literals,
    pipeline_stage_check_constraint_sql,
    pipeline_stage_display_label,
    pipeline_stages_ordered,
)

pytestmark = [pytest.mark.unit, pytest.mark.integration]


def test_every_stage_has_one_non_empty_label_in_order() -> None:
    ordered = pipeline_stages_ordered()
    assert tuple(ordered.keys()) == PIPELINE_STAGE_ORDER
    assert len(ordered) == len(set(ordered))
    for key, label in ordered.items():
        assert label == PIPELINE_STAGE_LABELS[key]
        assert label.strip()


def test_registry_modules_share_same_stage_keys() -> None:
    assert set(PIPELINE_STAGE_ORDER) == set(PIPELINE_STAGE_KEYS)
    assert set(PIPELINE_STAGE_ORDER) == set(ACQUISITION_PIPELINE_STAGES)
    assert STAGE_ORDER == PIPELINE_STAGE_ORDER


def test_registry_modules_share_same_labels() -> None:
    for key in PIPELINE_STAGE_ORDER:
        assert ACQUISITION_PIPELINE_STAGES[key] == PIPELINE_STAGE_LABELS[key]


def test_brief_status_mappings_resolve_to_valid_stages() -> None:
    assert BRIEF_STATUS_INITIAL_PIPELINE_STAGE["paid"] == "diagnostic_paid"
    assert BRIEF_STATUS_INITIAL_PIPELINE_STAGE["pending_payment"] == "qualified"
    assert BRIEF_STATUS_INITIAL_PIPELINE_STAGE["abandoned"] == "qualified"
    for stage in BRIEF_STATUS_INITIAL_PIPELINE_STAGE.values():
        assert stage in PIPELINE_STAGE_ORDER


def test_unknown_stage_label_falls_back_safely() -> None:
    assert pipeline_stage_display_label("custom_stage") == "Custom Stage"


def test_default_stage_is_first_in_order() -> None:
    assert DEFAULT_PIPELINE_STAGE == PIPELINE_STAGE_ORDER[0]


def test_terminal_and_side_exit_semantics() -> None:
    assert TERMINAL_STAGES == frozenset({"won", "lost"})
    assert SIDE_EXIT_STAGES == frozenset({"lost", "nurture"})
    assert TERMINAL_STAGES.issubset(set(PIPELINE_STAGE_ORDER))
    assert SIDE_EXIT_STAGES.issubset(set(PIPELINE_STAGE_ORDER))


def test_migration_check_constraint_matches_registry() -> None:
    pipeline = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    constraint_values = extract_pipeline_stage_check_values(pipeline.up_sql)
    assert constraint_values == pipeline_stage_check_constraint_literals()
    assert constraint_values == PIPELINE_STAGE_ORDER
    for stage in PIPELINE_STAGE_ORDER:
        assert f"'{stage}'" in pipeline.up_sql


def test_migration_helper_sql_literals_cover_all_stages() -> None:
    sql_literals = pipeline_stage_check_constraint_sql()
    for stage in PIPELINE_STAGE_ORDER:
        assert f"'{stage}'" in sql_literals


def test_registry_detects_constraint_drift() -> None:
    pipeline = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    parsed = set(extract_pipeline_stage_check_values(pipeline.up_sql))
    canonical = set(PIPELINE_STAGE_ORDER)
    assert parsed == canonical, (
        f"Schema/application drift: missing={sorted(canonical - parsed)!r}, "
        f"extra={sorted(parsed - canonical)!r}"
    )


def test_brief_status_and_stage_validation_fail_safely() -> None:
    from app.pipeline import InvalidStageError, initial_pipeline_stage_for_brief_status, validate_stage

    validate_stage("qualified")
    with pytest.raises(InvalidStageError):
        validate_stage("not-a-stage")
    with pytest.raises(InvalidStageError):
        initial_pipeline_stage_for_brief_status("unknown_status")
