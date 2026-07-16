"""Tests for deterministic ADMIN_PREVIEW_MODE preview context (#338)."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
)
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME_ISO,
    DEFAULT_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContextError,
    derive_fixture_rng,
    derive_fixture_seed,
    get_preview_context,
    load_preview_context,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_reproducibility_metadata,
    reset_preview_context_cache,
)


@pytest.fixture(autouse=True)
def _reset_preview_context(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_preview_context_cache()
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_FIXTURE_VERSION", raising=False)


@pytest.mark.unit
def test_default_context_uses_documented_seed_and_time() -> None:
    ctx = load_preview_context()
    assert ctx.root_seed == DEFAULT_PREVIEW_SEED
    assert ctx.reference_time == datetime.fromisoformat(DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_malformed_seed_raises() -> None:
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        parse_preview_seed("not-a-number")


@pytest.mark.unit
def test_malformed_reference_time_raises() -> None:
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_TIME"):
        parse_preview_reference_time("yesterday")


@pytest.mark.unit
def test_naive_reference_time_raises() -> None:
    with pytest.raises(PreviewContextError, match="timezone-aware"):
        parse_preview_reference_time("2026-07-15T12:00:00")


@pytest.mark.unit
def test_fixture_seed_is_order_independent_by_namespace() -> None:
    ctx = get_preview_context()
    companies_seed = derive_fixture_seed(
        ctx.root_seed, "companies", fixture_version=ctx.fixture_version
    )
    briefs_seed = derive_fixture_seed(
        ctx.root_seed, "brief_rows", fixture_version=ctx.fixture_version
    )
    assert companies_seed != briefs_seed


@pytest.mark.unit
def test_same_namespace_produces_deeply_equal_fixtures() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    rng = derive_fixture_rng("companies")
    a = build_preview_companies(rng=rng, now=now)
    b = build_preview_companies(rng=derive_fixture_rng("companies"), now=now)
    assert a == b


@pytest.mark.unit
def test_route_order_does_not_perturb_companies_fixture() -> None:
    baseline = build_preview_companies()
    build_preview_brief_rows()
    build_preview_acquisition_dashboard_data()
    after = build_preview_companies()
    assert baseline == after


@pytest.mark.unit
def test_unrelated_namespace_change_does_not_perturb_companies() -> None:
    before = build_preview_companies()
    build_preview_brief_rows()
    after = build_preview_companies()
    assert before == after


@pytest.mark.unit
def test_changing_root_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "1001")
    reset_preview_context_cache()
    alt_a = build_preview_companies()
    alt_b = build_preview_companies()
    assert alt_a == alt_b
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    reset_preview_context_cache()
    default = build_preview_companies()
    assert alt_a != default


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen.isoformat())
    reset_preview_context_cache()
    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen
    for row in data.recent_evidence:
        assert row.expires_at > frozen
    for row in data.stale_evidence:
        assert row.expires_at < frozen


@pytest.mark.unit
def test_preview_builders_do_not_call_wall_clock() -> None:
    import app.admin_preview as preview_module

    with patch.object(preview_module, "preview_now", wraps=preview_module.preview_now) as frozen:
        build_preview_companies()
        build_preview_brief_rows()
        build_preview_acquisition_dashboard_data()
        assert frozen.call_count >= 3
    assert "datetime.now" not in open(preview_module.__file__, encoding="utf-8").read()


@pytest.mark.unit
def test_reproducibility_metadata_excludes_secrets() -> None:
    meta = preview_reproducibility_metadata(
        head_sha="abc123",
        browser_version="120.0.0",
        viewports=("desktop", "mobile"),
    )
    assert meta["preview_seed"] == DEFAULT_PREVIEW_SEED
    assert meta["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert meta["head_sha"] == "abc123"
    assert meta["browser_version"] == "120.0.0"
    assert "password" not in json.dumps(meta).lower()
    assert "secret" not in json.dumps(meta).lower()


@pytest.mark.unit
def test_identical_fixtures_across_subprocesses() -> None:
    script = """
import json
import os
from app.admin_preview import build_preview_companies
from app.preview_context import reset_preview_context_cache

os.environ["ADMIN_PREVIEW_SEED"] = "42"
os.environ["ADMIN_PREVIEW_REFERENCE_TIME"] = "2026-07-15T12:00:00+00:00"
reset_preview_context_cache()
print(json.dumps(build_preview_companies(), default=str))
"""
    results = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        )
        results.append(json.loads(proc.stdout.strip()))
    assert results[0] == results[1]


@pytest.mark.unit
def test_explicit_rng_override_still_supported() -> None:
    fixed = random.Random(99)
    a = build_preview_brief_rows(rng=fixed)
    b = build_preview_brief_rows(rng=random.Random(99))
    assert a == b
