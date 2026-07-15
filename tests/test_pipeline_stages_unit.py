"""Unit tests for the canonical pipeline stage registry."""

from __future__ import annotations

import pytest

from app.migrations.definitions import MIGRATIONS
from app.pipeline import (
    InvalidStageError,
    initial_pipeline_stage_for_brief_status,
    pipeline_stage_label,
    validate_stage,
)
from app.pipeline_stages import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    PIPELINE_STAGE_LABELS,
    PIPELINE_STAGE_ORDER,
    PIPELINE_STAGES,
    SIDE_EXIT_STAGES,
    STAGE_ORDER,
    TERMINAL_STAGES,
    extract_pipeline_stage_check_values,
    is_valid_pipeline_stage,
    pipeline_stage_check_sql_fragment,
    resolve_initial_pipeline_stage_for_brief_status,
)

pytestmark = [pytest.mark.unit]


def test_every_stage_has_unique_nonempty_label_in_order() -> None:
    assert tuple(PIPELINE_STAGES.keys()) == PIPELINE_STAGE_ORDER
    assert len(PIPELINE_STAGE_ORDER) == len(set(PIPELINE_STAGE_ORDER))
    for stage in PIPELINE_STAGE_ORDER:
        label = PIPELINE_STAGE_LABELS[stage]
        assert label
        assert PIPELINE_STAGES[stage] == label


def test_main_stage_order_is_progression_subset() -> None:
    assert set(STAGE_ORDER).issubset(set(PIPELINE_STAGE_ORDER))
    assert "nurture" not in STAGE_ORDER
    assert STAGE_ORDER.index("won") == len(STAGE_ORDER) - 1


def test_terminal_and_side_exit_semantics() -> None:
    assert TERMINAL_STAGES == frozenset({"won", "lost"})
    assert SIDE_EXIT_STAGES == frozenset({"lost", "nurture"})
    assert TERMINAL_STAGES.issubset(set(PIPELINE_STAGE_ORDER))
    assert SIDE_EXIT_STAGES.issubset(set(PIPELINE_STAGE_ORDER))


def test_brief_status_mappings_resolve_to_canonical_stages() -> None:
    for brief_status, stage in BRIEF_STATUS_INITIAL_PIPELINE_STAGE.items():
        assert is_valid_pipeline_stage(stage)
        assert initial_pipeline_stage_for_brief_status(brief_status) == stage


def test_unknown_brief_status_and_stage_fail_safely() -> None:
    assert resolve_initial_pipeline_stage_for_brief_status("unknown_status") is None
    with pytest.raises(InvalidStageError, match="Unsupported brief status"):
        initial_pipeline_stage_for_brief_status("unknown_status")
    with pytest.raises(InvalidStageError, match="Unsupported pipeline stage"):
        validate_stage("proposal")
    assert not is_valid_pipeline_stage("proposal")


def test_pipeline_stage_label_handles_empty_and_unknown() -> None:
    assert pipeline_stage_label("qualified") == "Qualified"
    assert pipeline_stage_label("") == "—"
    assert pipeline_stage_label(None) == "—"
    assert pipeline_stage_label("custom_stage") == "Custom Stage"


def test_migration_check_constraint_matches_canonical_registry() -> None:
    migration = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    schema_values = extract_pipeline_stage_check_values(migration.up_sql)
    assert schema_values == frozenset(PIPELINE_STAGE_ORDER)


def test_migration_helper_matches_landed_constraint() -> None:
    migration = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    schema_values = extract_pipeline_stage_check_values(migration.up_sql)
    helper_values = extract_pipeline_stage_check_values(pipeline_stage_check_sql_fragment())
    assert helper_values == schema_values


def test_preview_registry_imports_canonical_stages() -> None:
    from app.admin_preview import PIPELINE_STAGES as preview_stages

    assert tuple(preview_stages.keys()) == PIPELINE_STAGE_ORDER
    assert preview_stages == PIPELINE_STAGES


def test_acquisition_pipeline_imports_canonical_stages() -> None:
    from app.acquisition_pipeline import PIPELINE_STAGES as transition_stages

    assert tuple(transition_stages.keys()) == PIPELINE_STAGE_ORDER
    assert transition_stages == PIPELINE_STAGES
