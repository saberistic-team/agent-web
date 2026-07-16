"""Deterministic ADMIN_PREVIEW_MODE fixture contract (#338)."""

from __future__ import annotations

import json
import multiprocessing
import os
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewConfigError,
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_dashboard_data,
    build_preview_section_rows,
    get_preview_context,
    load_preview_context,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_reproducibility_fields,
    reset_preview_context_cache,
)
from app.main import app


def _enable_preview_env(monkeypatch: pytest.MonkeyPatch, *, seed: str | None = None) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_FIXTURE_VERSION", raising=False)
    reset_preview_context_cache()
    if seed is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
        reset_preview_context_cache()


@pytest.mark.unit
def test_same_context_route_deeply_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_preview_env(monkeypatch)
    a = build_preview_dashboard_data()
    b = build_preview_dashboard_data()
    assert a == b
    assert a.generated_at == DEFAULT_PREVIEW_REFERENCE_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")


@pytest.mark.unit
def test_route_independent_of_other_route_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_preview_env(monkeypatch)
    build_preview_acquisition_dashboard_data()
    build_preview_section_rows("/admin/signals")
    build_preview_brief_rows()
    after = build_preview_companies()
    reset_preview_context_cache()
    _enable_preview_env(monkeypatch)
    baseline = build_preview_companies()
    assert after == baseline


@pytest.mark.unit
def test_namespace_change_does_not_perturb_other_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_preview_env(monkeypatch)
    companies_before = build_preview_companies()
    signals_before = build_preview_section_rows("/admin/signals")
    build_preview_section_rows("/admin/analytics")
    companies_after = build_preview_companies()
    signals_after = build_preview_section_rows("/admin/signals")
    assert companies_before == companies_after
    assert signals_before == signals_after
    assert signals_before != build_preview_section_rows("/admin/analytics")


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_preview_env(monkeypatch, seed="100")
    first_a = build_preview_dashboard_data()
    first_b = build_preview_dashboard_data()
    assert first_a == first_b

    _enable_preview_env(monkeypatch, seed="101")
    second_a = build_preview_dashboard_data()
    second_b = build_preview_dashboard_data()
    assert second_a == second_b
    assert first_a != second_a


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen.isoformat())
    reset_preview_context_cache()
    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen


@pytest.mark.unit
def test_preview_builders_avoid_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_preview_env(monkeypatch)
    with patch("app.admin_preview.datetime") as mock_dt:
        mock_dt.now.side_effect = AssertionError("datetime.now must not be called")
        mock_dt.side_effect = datetime
        build_preview_dashboard_data()
        build_preview_brief_rows()
        build_preview_companies()


@pytest.mark.unit
def test_malformed_seed_fails_fast() -> None:
    with pytest.raises(PreviewConfigError):
        parse_preview_seed("not-a-number")


@pytest.mark.unit
def test_malformed_reference_time_fails_fast() -> None:
    with pytest.raises(PreviewConfigError):
        parse_preview_reference_time("yesterday")


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_preview_env(monkeypatch)
    ctx = load_preview_context()
    assert ctx is not None
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_preview_reproducibility_fields_non_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_preview_env(monkeypatch, seed="55")
    fields = preview_reproducibility_fields()
    assert fields["fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert fields["preview_seed"] == "55"
    assert "preview_reference_time" in fields
    assert "secret" not in json.dumps(fields).lower()


def _worker_build_companies(_: int) -> list[dict[str, object]]:
    os.environ["ADMIN_PREVIEW_MODE"] = "1"
    reset_preview_context_cache()
    return build_preview_companies()


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_preview_env(monkeypatch)
    expected = build_preview_companies()
    with multiprocessing.Pool(processes=2) as pool:
        results = pool.map(_worker_build_companies, [0, 1])
    assert results[0] == expected
    assert results[1] == expected


@pytest.mark.integration
def test_desktop_and_mobile_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    _enable_preview_env(monkeypatch, seed="42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    client = TestClient(app, follow_redirects=False)
    first = client.get("/admin/companies")
    second = client.get("/admin/briefs")
    third = client.get("/admin/companies")
    assert first.status_code == 200
    assert third.status_code == 200
    assert first.text == third.text
    assert "Preview data — not production" in first.text
    assert second.status_code == 200
    assert "brief-table" in second.text


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screenshot_deploy import (
        build_screenshot_reproducibility_manifest,
        format_reproducibility_comment_lines,
        preview_server_env,
    )

    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    reset_preview_context_cache()

    env = preview_server_env({})
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_TIME"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert env["ADMIN_PREVIEW_FIXTURE_VERSION"] == PREVIEW_FIXTURE_VERSION

    manifest = build_screenshot_reproducibility_manifest(
        viewports=[("desktop", 1280, 800), ("mobile", 390, 844)],
        browser_version="Chromium 120.0.0.0",
        head_sha="abc123",
    )
    assert manifest["preview_seed"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert manifest["fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["browser_version"] == "Chromium 120.0.0.0"
    assert manifest["head_sha"] == "abc123"
    lines = format_reproducibility_comment_lines(manifest)
    assert any("preview reproducibility" in line for line in lines)
    assert any("fixture_version" in line for line in lines)
    assert any("browser_version" in line for line in lines)


@pytest.mark.unit
def test_get_preview_context_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_preview_env(monkeypatch, seed="9")
    first = get_preview_context()
    second = get_preview_context()
    assert first is second
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "10")
    still_cached = get_preview_context()
    assert still_cached is first
    reset_preview_context_cache()
    refreshed = get_preview_context()
    assert refreshed is not None
    assert refreshed.root_seed == 10
