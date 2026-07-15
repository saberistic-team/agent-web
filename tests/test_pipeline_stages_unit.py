"""Unit tests for the canonical pipeline stage registry."""

from __future__ import annotations

import pytest

from app.migrations.definitions import MIGRATIONS
from app.pipeline_stages import (
    BRIEF_STATUS_INITIAL_PIPELINE_STAGE,
    InvalidStageError,
    PIPELINE_STAGE_KEYS,
    PIPELINE_STAGES,
    SIDE_EXIT_STAGES,
    STAGE_ORDER,
    TERMINAL_STAGES,
    initial_pipeline_stage_for_brief_status,
    is_valid_pipeline_stage,
    parse_pipeline_stage_check_constraint,
    pipeline_stage_db_check_values,
    pipeline_stage_label,
    validate_pipeline_stage,
    validate_stage,
)

pytestmark = [pytest.mark.unit, pytest.mark.integration]


def test_every_stage_has_unique_nonempty_label_in_registry_order() -> None:
    assert len(PIPELINE_STAGE_KEYS) == len(PIPELINE_STAGES)
    assert len(set(PIPELINE_STAGE_KEYS)) == len(PIPELINE_STAGE_KEYS)
    for key in PIPELINE_STAGE_KEYS:
        label = PIPELINE_STAGES[key]
        assert label
        assert label == label.strip()


def test_stage_order_is_linear_progression_without_side_exits() -> None:
    assert STAGE_ORDER == tuple(key for key in PIPELINE_STAGE_KEYS if key not in SIDE_EXIT_STAGES)
    assert SIDE_EXIT_STAGES == frozenset({"lost", "nurture"})
    assert TERMINAL_STAGES == frozenset({"won", "lost"})


def test_brief_status_mappings_resolve_to_canonical_stages() -> None:
    assert initial_pipeline_stage_for_brief_status("paid") == "diagnostic_paid"
    assert initial_pipeline_stage_for_brief_status("pending_payment") == "qualified"
    assert initial_pipeline_stage_for_brief_status("abandoned") == "qualified"
    for stage in BRIEF_STATUS_INITIAL_PIPELINE_STAGE.values():
        assert is_valid_pipeline_stage(stage)


def test_unknown_brief_status_and_stage_fail_safely() -> None:
    with pytest.raises(InvalidStageError, match="Unsupported brief status"):
        initial_pipeline_stage_for_brief_status("unknown_status")
    with pytest.raises(InvalidStageError, match="Unsupported pipeline stage"):
        validate_stage("not-a-stage")
    with pytest.raises(ValueError, match="Invalid pipeline stage"):
        validate_pipeline_stage("proposal")


def test_pipeline_stage_label_handles_empty_and_unknown() -> None:
    assert pipeline_stage_label("qualified") == "Qualified"
    assert pipeline_stage_label("") == "—"
    assert pipeline_stage_label("custom_stage") == "Custom Stage"


def test_migration_check_constraint_matches_canonical_registry() -> None:
    migration = next(m for m in MIGRATIONS if m.name == "acquisition_pipeline")
    db_values = parse_pipeline_stage_check_constraint(migration.up_sql)
    assert db_values == frozenset(pipeline_stage_db_check_values())
    assert tuple(sorted(db_values)) == tuple(sorted(PIPELINE_STAGE_KEYS))


def test_preview_and_application_registries_share_stage_keys() -> None:
    from app.admin_preview import build_preview_pipeline_companies

    preview = build_preview_pipeline_companies(rng=__import__("random").Random(42))
    preview_stages = {str(row["pipeline_stage"]) for row in preview}
    assert preview_stages.issubset(set(PIPELINE_STAGE_KEYS))
