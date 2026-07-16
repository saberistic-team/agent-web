"""Deterministic ADMIN_PREVIEW_MODE fixture contract (issue #338)."""

from __future__ import annotations

import multiprocessing as mp
import os
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_companies,
    build_preview_pipeline_companies,
    preview_contact_restore_conflict,
)
from app.main import app
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_SEED,
    ENV_PREVIEW_REFERENCE_TIME,
    ENV_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    clear_preview_context_cache,
    derive_namespace_seed,
    load_preview_context_from_env,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_rng_for,
    preview_server_env_defaults,
    set_preview_context,
)


def _worker_build_companies(_: int) -> list[dict[str, object]]:
    set_preview_context(
        PreviewContext(
            seed=77,
            reference_time=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            fixture_version=PREVIEW_FIXTURE_VERSION,
        )
    )
    return build_preview_companies()


@pytest.fixture(autouse=True)
def _reset_preview_context() -> None:
    clear_preview_context_cache()
    yield
    clear_preview_context_cache()


def _frozen_context(*, seed: int = 42) -> PreviewContext:
    return PreviewContext(
        seed=seed,
        reference_time=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )


@pytest.mark.unit
def test_same_context_and_route_deeply_equal_across_calls() -> None:
    ctx = _frozen_context()
    set_preview_context(ctx)
    a = build_preview_companies()
    b = build_preview_companies()
    assert a == b
    assert len(a) == 5


@pytest.mark.unit
def test_same_route_identical_when_other_fixtures_requested_first() -> None:
    ctx = _frozen_context(seed=99)
    set_preview_context(ctx)
    _ = build_preview_pipeline_companies()
    _ = build_preview_acquisition_dashboard_data()
    first = build_preview_companies()
    _ = build_preview_acquisition_dashboard_data()
    second = build_preview_companies()
    assert first == second


@pytest.mark.unit
def test_desktop_and_mobile_http_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    for key, value in preview_server_env_defaults().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    desktop = client.get("/admin/companies")
    mobile = client.get("/admin/companies")
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text
    assert "Northwind Labs" in desktop.text


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures() -> None:
    with mp.Pool(processes=2) as pool:
        results = pool.map(_worker_build_companies, [0, 1])
    assert results[0] == results[1]


@pytest.mark.unit
def test_namespace_isolation_between_routes() -> None:
    ctx = _frozen_context(seed=55)
    set_preview_context(ctx)
    companies = build_preview_companies()
    pipeline = build_preview_pipeline_companies()
    companies_again = build_preview_companies()
    assert companies == companies_again
    assert companies != pipeline


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_companies(
        rng=preview_rng_for("companies", context=_frozen_context(seed=1)),
        now=now,
    )
    b = build_preview_companies(
        rng=preview_rng_for("companies", context=_frozen_context(seed=1)),
        now=now,
    )
    c = build_preview_companies(
        rng=preview_rng_for("companies", context=_frozen_context(seed=2)),
        now=now,
    )
    assert a == b
    assert a != c


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_freshness_boundaries() -> None:
    ref = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(seed=42, reference_time=ref)
    data = build_preview_acquisition_dashboard_data(
        rng=preview_rng_for("acquisition_dashboard", context=ctx),
        now=ref,
    )
    assert data.generated_at == ref
    assert data.overdue_actions[0].next_action_due_at < ref
    assert data.upcoming_actions[0].next_action_due_at > ref
    companies = build_preview_companies(
        rng=preview_rng_for("companies", context=ctx),
        now=ref,
    )
    fresh = [row for row in companies if row.get("last_verified_at")]
    stale = [row for row in companies if not row.get("last_verified_at")]
    assert fresh or stale


@pytest.mark.unit
def test_preview_builders_do_not_read_wall_clock_when_context_present() -> None:
    ctx = _frozen_context()
    set_preview_context(ctx)
    with patch("app.preview_context.datetime") as mocked_dt:
        mocked_dt.now.side_effect = AssertionError("wall-clock read in preview")
        mocked_dt.side_effect = AssertionError("wall-clock read in preview")
        build_preview_companies()
        build_preview_acquisition_dashboard_data()


@pytest.mark.unit
def test_screenshot_reproducibility_manifest_records_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.screenshot_deploy import build_screenshot_reproducibility_manifest

    monkeypatch.setenv(ENV_PREVIEW_SEED, "338")
    monkeypatch.setenv(
        ENV_PREVIEW_REFERENCE_TIME, DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    )
    manifest = build_screenshot_reproducibility_manifest(
        browser_version="120.0.0",
        viewports=[{"name": "desktop", "width": 1280, "height": 800}],
    )
    assert manifest["preview_seed"] == 338
    assert manifest["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["browser"] == "chromium"
    assert manifest["browser_version"] == "120.0.0"
    assert manifest["viewports"] == [{"name": "desktop", "width": 1280, "height": 800}]
    assert "ADMIN_SESSION_SECRET" not in str(manifest)


@pytest.mark.unit
def test_missing_preview_values_use_stable_defaults() -> None:
    assert parse_preview_seed(None) == DEFAULT_PREVIEW_SEED
    assert parse_preview_reference_time(None) == DEFAULT_PREVIEW_REFERENCE_TIME
    defaults = preview_server_env_defaults()
    assert defaults[ENV_PREVIEW_SEED] == str(DEFAULT_PREVIEW_SEED)
    assert defaults[ENV_PREVIEW_REFERENCE_TIME] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()


@pytest.mark.unit
def test_malformed_preview_configuration_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PREVIEW_SEED, "not-a-number")
    with pytest.raises(PreviewContextError):
        load_preview_context_from_env()
    monkeypatch.setenv(ENV_PREVIEW_SEED, "42")
    monkeypatch.setenv(ENV_PREVIEW_REFERENCE_TIME, "yesterday")
    with pytest.raises(PreviewContextError):
        load_preview_context_from_env()


@pytest.mark.unit
def test_preview_server_env_overrides_include_seed_and_time() -> None:
    from scripts.screenshot_deploy import preview_server_env_overrides

    env = preview_server_env_overrides()
    assert env[ENV_PREVIEW_SEED] == str(DEFAULT_PREVIEW_SEED)
    assert env[ENV_PREVIEW_REFERENCE_TIME] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()


@pytest.mark.unit
def test_derive_namespace_seed_changes_only_target_namespace() -> None:
    root = 338
    companies_a = derive_namespace_seed(root, "companies")
    companies_b = derive_namespace_seed(root, "companies")
    pipeline = derive_namespace_seed(root, "pipeline_companies")
    assert companies_a == companies_b
    assert companies_a != pipeline


@pytest.mark.unit
def test_restore_conflict_uses_frozen_reference_time() -> None:
    ref = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    set_preview_context(PreviewContext(seed=7, reference_time=ref))
    payload = preview_contact_restore_conflict()
    archived_at = datetime.fromisoformat(str(payload["archived_contact"]["archived_at"]))
    assert archived_at <= ref
    assert archived_at >= ref - timedelta(days=30)
