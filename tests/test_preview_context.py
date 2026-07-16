"""Deterministic ADMIN_PREVIEW_MODE fixture context (#338)."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    build_preview_pipeline_companies,
    build_preview_section_rows,
)
from app.main import app
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME_ISO,
    DEFAULT_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    derive_fixture_rng,
    derive_fixture_seed,
    fixture_now,
    fixture_rng,
    parse_preview_reference_time,
    parse_preview_seed,
    reset_preview_context_cache,
    resolve_preview_context,
)


@pytest.fixture(autouse=True)
def _clear_preview_context_cache() -> None:
    reset_preview_context_cache()
    yield
    reset_preview_context_cache()


@pytest.mark.unit
def test_same_context_and_route_produces_deeply_equal_fixtures() -> None:
    ctx = PreviewContext(
        root_seed=42,
        reference_time=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    a = build_preview_companies(context=ctx)
    b = build_preview_companies(context=ctx)
    assert a == b
    assert len(a) >= 4


@pytest.mark.unit
def test_same_route_identical_when_other_routes_requested_first() -> None:
    ctx = PreviewContext(
        root_seed=99,
        reference_time=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    _ = build_preview_brief_rows(context=ctx)
    _ = build_preview_section_rows("/admin/signals", context=ctx)
    first = build_preview_companies(context=ctx)
    _ = build_preview_contacts(context=ctx)
    second = build_preview_companies(context=ctx)
    assert first == second


@pytest.mark.unit
def test_desktop_and_mobile_http_responses_share_fixture_data(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T12:00:00+00:00")
    client = TestClient(app)
    cookie = {"admin_session": "preview-screenshot-session"}
    desktop = client.get("/admin/companies", cookies=cookie)
    mobile = client.get("/admin/companies", cookies=cookie)
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


@pytest.mark.unit
def test_multiple_worker_processes_produce_identical_fixtures() -> None:
    ctx = PreviewContext(
        root_seed=7,
        reference_time=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    code = """
import json
from datetime import datetime, timezone
from app.admin_preview import build_preview_pipeline_companies
from app.preview_context import PreviewContext

ctx = PreviewContext(
    root_seed=7,
    reference_time=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
)
print(json.dumps(build_preview_pipeline_companies(context=ctx), default=str))
"""
    outputs = [
        subprocess.check_output([sys.executable, "-c", code], text=True)
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1]


@pytest.mark.unit
def test_namespace_isolation_and_seed_alternates() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(root_seed=100, reference_time=frozen)
    companies_a = build_preview_companies(context=ctx)
    briefs_a = build_preview_brief_rows(context=ctx)
    signals_a = build_preview_section_rows("/admin/signals", context=ctx)
    _ = build_preview_section_rows("/admin/analytics", context=ctx)
    companies_b = build_preview_companies(context=ctx)
    signals_b = build_preview_section_rows("/admin/signals", context=ctx)
    assert companies_a == companies_b
    assert signals_a == signals_b
    assert signals_a != build_preview_section_rows("/admin/analytics", context=ctx)
    assert briefs_a != build_preview_brief_rows(
        context=PreviewContext(root_seed=200, reference_time=frozen)
    )


@pytest.mark.unit
def test_changing_namespace_derivation_isolates_fixtures() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    seed_a = derive_fixture_seed(42, "admin.companies.list")
    seed_b = derive_fixture_seed(42, "admin.briefs.list")
    assert seed_a != seed_b
    assert fixture_rng("admin.companies.list", context=PreviewContext(42, frozen)).randint(
        0, 999
    ) != fixture_rng("admin.briefs.list", context=PreviewContext(42, frozen)).randint(
        0, 999
    )


@pytest.mark.unit
def test_frozen_time_controls_overdue_upcoming_and_date_fields() -> None:
    from app.admin_preview import build_preview_acquisition_dashboard_data

    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(root_seed=11, reference_time=frozen)
    dashboard = build_preview_acquisition_dashboard_data(context=ctx)
    assert dashboard.generated_at == frozen
    for row in dashboard.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in dashboard.upcoming_actions:
        assert row.next_action_due_at > frozen
    pipeline = build_preview_pipeline_companies(context=ctx)
    for row in pipeline:
        due = row["next_action_due_at"]
        assert isinstance(due, datetime)
        assert abs((due - frozen).days) <= 12


@pytest.mark.unit
def test_fixture_now_never_calls_wall_clock_when_context_present() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(root_seed=1, reference_time=frozen)
    with mock.patch("app.preview_context.datetime") as mocked_dt:
        mocked_dt.now.side_effect = AssertionError("wall clock must not be read")
        assert fixture_now(context=ctx) == frozen
        build_preview_companies(context=ctx)


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields(
    tmp_path, monkeypatch
) -> None:
    from screenshot_deploy import write_capture_reproducibility_manifest

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "338042")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    reset_preview_context_cache()
    dest = write_capture_reproducibility_manifest(
        tmp_path,
        phase="branch",
        browser_version="Chromium 120.0.0.0",
        head_sha="abc123",
    )
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert payload["preview_root_seed"] == 338042
    assert payload["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    assert payload["head_sha"] == "abc123"
    assert payload["browser_version"] == "Chromium 120.0.0.0"
    assert payload["viewports"]


@pytest.mark.unit
def test_malformed_seed_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "")
    with pytest.raises(PreviewContextError):
        resolve_preview_context()


@pytest.mark.unit
def test_malformed_reference_time_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T12:00:00")
    with pytest.raises(PreviewContextError):
        resolve_preview_context()


@pytest.mark.unit
def test_missing_env_uses_stable_defaults() -> None:
    ctx = resolve_preview_context()
    assert ctx.root_seed == DEFAULT_PREVIEW_SEED
    assert ctx.reference_time == parse_preview_reference_time(None)
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_derive_fixture_seed_is_order_independent() -> None:
    a = derive_fixture_seed(42, "admin.companies.list")
    b = derive_fixture_seed(42, "admin.briefs.list")
    c = derive_fixture_seed(42, "admin.companies.list")
    assert a == c
    assert a != b
    rng_a = derive_fixture_rng(42, "admin.companies.list")
    rng_b = derive_fixture_rng(42, "admin.companies.list")
    assert [rng_a.randint(0, 999) for _ in range(5)] == [
        rng_b.randint(0, 999) for _ in range(5)
    ]


@pytest.mark.unit
def test_preview_capture_env_sets_seed_and_reference_time(monkeypatch) -> None:
    from screenshot_deploy import preview_capture_env

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "55")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-01-02T03:04:05+00:00")
    reset_preview_context_cache()
    env = preview_capture_env(base_url="http://127.0.0.1:8765")
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["ADMIN_PREVIEW_SEED"] == "55"
    assert env["ADMIN_PREVIEW_REFERENCE_TIME"] == "2026-01-02T03:04:05+00:00"
    assert env["ADMIN_PREVIEW_FIXTURE_VERSION"] == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_explicit_rng_override_still_supported() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    a = build_preview_brief_rows(rng=random.Random(123), now=frozen)
    b = build_preview_brief_rows(rng=random.Random(123), now=frozen)
    assert a == b
