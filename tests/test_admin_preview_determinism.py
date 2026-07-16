"""Deterministic preview fixture contract for issue #338."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    build_preview_pipeline_companies,
    clear_preview_context_cache,
    preview_namespace_seed,
    preview_rng_for,
    resolve_preview_context,
)
from app.main import app


def _preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: str = "",
    reference_time: str = "",
    fixture_version: str = "",
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    if seed:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    if reference_time:
        monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", reference_time)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    if fixture_version:
        monkeypatch.setenv("ADMIN_PREVIEW_FIXTURE_VERSION", fixture_version)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_FIXTURE_VERSION", raising=False)
    clear_preview_context_cache()


@pytest.fixture(autouse=True)
def _reset_preview_context() -> None:
    clear_preview_context_cache()
    yield
    clear_preview_context_cache()


@pytest.mark.unit
def test_same_context_and_route_deeply_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    a = build_preview_companies()
    b = build_preview_companies()
    assert a == b
    assert a


@pytest.mark.unit
def test_route_identical_regardless_of_other_route_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    first = build_preview_brief_rows()
    build_preview_contacts()
    build_preview_pipeline_companies()
    second = build_preview_brief_rows()
    assert first == second


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_and_mobile_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="42")
    client = TestClient(app, follow_redirects=False)
    desktop = client.get("/admin/companies")
    mobile = client.get("/admin/companies")
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text
    assert "Preview data — not production" in desktop.text


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preview_env(monkeypatch)
    repo_root = Path(__file__).resolve().parents[1]
    script = """
import json
import os
os.environ["ADMIN_PREVIEW_MODE"] = "1"
os.environ["BASE_URL"] = "http://127.0.0.1:8765"
from app.admin_preview import build_preview_brief_rows, clear_preview_context_cache
clear_preview_context_cache()
rows = build_preview_brief_rows()
print(json.dumps(rows, default=str))
"""
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    outputs: list[str] = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=repo_root,
            check=True,
        )
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1]


@pytest.mark.unit
def test_namespace_isolation_and_unrelated_fixture_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    baseline_companies = build_preview_companies()
    baseline_briefs = build_preview_brief_rows()
    build_preview_contacts()
    build_preview_pipeline_companies(stage_filter="qualified")
    assert build_preview_companies() == baseline_companies
    assert build_preview_brief_rows() == baseline_briefs
    other_seed = preview_namespace_seed(
        DEFAULT_PREVIEW_SEED,
        "companies",
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    assert other_seed != preview_namespace_seed(
        DEFAULT_PREVIEW_SEED,
        "brief_rows",
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="100")
    seeded_a = build_preview_brief_rows()
    seeded_b = build_preview_brief_rows()
    _preview_env(monkeypatch, seed="200")
    alternate_a = build_preview_brief_rows()
    alternate_b = build_preview_brief_rows()
    assert seeded_a == seeded_b
    assert alternate_a == alternate_b
    assert seeded_a != alternate_a


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    _preview_env(monkeypatch, reference_time=frozen.isoformat())
    from app.admin_preview import build_preview_acquisition_dashboard_data

    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
        assert row.next_action_due_at >= frozen - timedelta(days=10)
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen
        assert row.next_action_due_at <= frozen + timedelta(days=10)


@pytest.mark.unit
def test_preview_builders_do_not_read_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    sentinel = datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, N805
            raise AssertionError("datetime.now called while preview context is active")

    monkeypatch.setattr("app.admin_preview.datetime", _FrozenDateTime)
    rows = build_preview_brief_rows()
    assert rows


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    from screenshot_deploy import build_preview_reproducibility_manifest

    manifest = build_preview_reproducibility_manifest(
        phase="branch",
        browser_version="Chromium 120.0.0.0",
        head_sha="abc123",
        pr_number=338,
    )
    assert manifest["preview_seed"] == DEFAULT_PREVIEW_SEED
    assert manifest["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["browser"] == "Chromium 120.0.0.0"
    assert manifest["head_sha"] == "abc123"
    assert manifest["pr_number"] == 338
    assert manifest["viewports"]


@pytest.mark.unit
def test_missing_ci_values_use_stable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    context = resolve_preview_context()
    assert context is not None
    assert context.seed == DEFAULT_PREVIEW_SEED
    assert context.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert context.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_malformed_seed_or_timestamp_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "not-a-seed")
    clear_preview_context_cache()
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        resolve_preview_context()

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "yesterday")
    clear_preview_context_cache()
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_TIME"):
        resolve_preview_context()


@pytest.mark.unit
def test_preview_server_env_sets_seed_and_reference_time() -> None:
    from screenshot_deploy import (
        DEFAULT_PREVIEW_REFERENCE_TIME,
        DEFAULT_PREVIEW_SEED,
        preview_server_env,
    )

    env = preview_server_env({})
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_TIME"] == DEFAULT_PREVIEW_REFERENCE_TIME


@pytest.mark.unit
def test_explicit_override_remains_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="999", reference_time="2025-01-15T08:30:00+00:00")
    context = resolve_preview_context()
    assert context is not None
    assert context.seed == 999
    assert context.reference_time == datetime(2025, 1, 15, 8, 30, tzinfo=timezone.utc)
    rows = build_preview_brief_rows()
    assert rows == build_preview_brief_rows()


@pytest.mark.unit
def test_preview_rng_for_matches_documented_construction() -> None:
    context = PreviewContext(seed=42, reference_time=DEFAULT_PREVIEW_REFERENCE_TIME)
    expected_seed = preview_namespace_seed(42, "brief_rows")
    assert preview_rng_for(context, "brief_rows").randint(0, 10_000) == (
        __import__("random").Random(expected_seed).randint(0, 10_000)
    )
