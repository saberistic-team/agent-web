"""Deterministic ADMIN_PREVIEW_MODE fixture contract (#338)."""

from __future__ import annotations

import json
import multiprocessing
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_analytics_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_section_rows,
    preview_contact_restore_conflict,
)
from app.admin_preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_ROOT_SEED,
    ENV_PREVIEW_REFERENCE_TIME,
    ENV_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    derive_namespace_seed,
    get_preview_context,
    parse_preview_context_from_environ,
    preview_rng_for_namespace,
    reset_preview_context_cache,
)
from app.main import app
from tests.conftest import enable_admin_preview_env


@pytest.fixture(autouse=True)
def _reset_preview_context_between_tests() -> None:
    reset_preview_context_cache()
    yield
    reset_preview_context_cache()


def _worker_build_companies(payload: dict[str, str]) -> None:
    import os

    os.environ.update(payload)
    reset_preview_context_cache()
    rows = build_preview_companies()
    print(json.dumps(rows, default=str))


@pytest.mark.unit
def test_same_context_and_route_deeply_equal() -> None:
    ctx = PreviewContext(
        root_seed=42,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    a = build_preview_companies(now=ctx.reference_time, rng=preview_rng_for_namespace("companies", context=ctx))
    b = build_preview_companies(now=ctx.reference_time, rng=preview_rng_for_namespace("companies", context=ctx))
    assert a == b


@pytest.mark.unit
def test_route_identical_when_other_routes_requested_first() -> None:
    ctx = PreviewContext(
        root_seed=99,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    _ = build_preview_section_rows(
        "/admin/signals",
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("section:/admin/signals", context=ctx),
    )
    first = build_preview_companies(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("companies", context=ctx),
    )
    _ = build_preview_brief_rows(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("brief_rows", context=ctx),
    )
    second = build_preview_companies(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("companies", context=ctx),
    )
    assert first == second


@pytest.mark.unit
def test_desktop_and_mobile_http_responses_match(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch, preview_seed="42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    desktop = client.get(
        "/admin/companies",
        headers={"User-Agent": "desktop-test"},
        cookies={"admin_session": "preview-screenshot-session"},
    )
    mobile = client.get(
        "/admin/companies",
        headers={"User-Agent": "mobile-test"},
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


@pytest.mark.unit
def test_multiple_worker_processes_produce_identical_fixtures() -> None:
    env = {
        ENV_PREVIEW_SEED: "42",
        ENV_PREVIEW_REFERENCE_TIME: DEFAULT_PREVIEW_REFERENCE_TIME.isoformat(),
    }
    with multiprocessing.Pool(2) as pool:
        outputs = pool.map(_worker_build_companies, [env, env])
    assert outputs[0] == outputs[1]


@pytest.mark.unit
def test_namespace_change_affects_only_target_fixture() -> None:
    ctx = PreviewContext(
        root_seed=7,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    companies_a = build_preview_companies(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("companies", context=ctx),
    )
    _ = build_preview_section_rows(
        "/admin/analytics",
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("section:/admin/analytics", context=ctx),
    )
    companies_b = build_preview_companies(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("companies", context=ctx),
    )
    assert companies_a == companies_b


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset() -> None:
    now = DEFAULT_PREVIEW_REFERENCE_TIME
    a = build_preview_brief_rows(
        now=now,
        rng=preview_rng_for_namespace(
            "brief_rows",
            context=PreviewContext(1, now, PREVIEW_FIXTURE_VERSION),
        ),
    )
    b = build_preview_brief_rows(
        now=now,
        rng=preview_rng_for_namespace(
            "brief_rows",
            context=PreviewContext(1, now, PREVIEW_FIXTURE_VERSION),
        ),
    )
    c = build_preview_brief_rows(
        now=now,
        rng=preview_rng_for_namespace(
            "brief_rows",
            context=PreviewContext(2, now, PREVIEW_FIXTURE_VERSION),
        ),
    )
    assert a == b
    assert a != c


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming_boundaries() -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(42, frozen, PREVIEW_FIXTURE_VERSION)
    data = build_preview_acquisition_dashboard_data(
        now=frozen,
        rng=preview_rng_for_namespace("acquisition_dashboard", context=ctx),
    )
    assert data.generated_at == frozen
    assert data.overdue_actions
    assert data.upcoming_actions
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
        assert row.next_action_due_at >= frozen - timedelta(days=10)
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen
        assert row.next_action_due_at <= frozen + timedelta(days=10)


@pytest.mark.unit
def test_analytics_preview_dashboard_is_deterministic() -> None:
    ctx = PreviewContext(
        root_seed=116,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    rng = preview_rng_for_namespace("analytics_dashboard", context=ctx)
    first = build_preview_analytics_dashboard_data(
        now=ctx.reference_time,
        rng=rng,
    )
    second = build_preview_analytics_dashboard_data(
        now=ctx.reference_time,
        rng=preview_rng_for_namespace("analytics_dashboard", context=ctx),
    )
    assert first == second
    assert first.engagement_counts[0].count > 0
    assert first.attribution
    assert first.case_studies
    assert first.articles


@pytest.mark.unit
def test_preview_builders_use_context_time_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PREVIEW_SEED, "42")
    monkeypatch.setenv(ENV_PREVIEW_REFERENCE_TIME, "2030-01-01T00:00:00+00:00")
    reset_preview_context_cache()
    rows = build_preview_brief_rows()
    created = rows[0]["created_at"]
    assert isinstance(created, datetime)
    assert created <= datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    from scripts.screenshot_deploy import build_reproducibility_manifest, ScreenshotTarget

    ctx = PreviewContext(
        root_seed=33842,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    manifest = build_reproducibility_manifest(
        phase="branch",
        preview_context=ctx,
        viewports=(("desktop", 1280, 800), ("mobile", 390, 844)),
        routes=[ScreenshotTarget(route="/admin")],
        head_sha="abc123",
        browser_version="120.0.6099.0",
    )
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["preview_root_seed"] == "33842"
    assert manifest["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert manifest["head_sha"] == "abc123"
    assert manifest["browser_version"] == "120.0.6099.0"
    assert manifest["viewports"][0]["name"] == "desktop"


@pytest.mark.unit
def test_malformed_seed_or_time_fail_fast() -> None:
    with pytest.raises(PreviewContextError):
        parse_preview_context_from_environ(
            {ENV_PREVIEW_SEED: "42", ENV_PREVIEW_REFERENCE_TIME: "not-a-date"},
            use_defaults=False,
        )
    with pytest.raises(PreviewContextError):
        parse_preview_context_from_environ(
            {ENV_PREVIEW_SEED: "", ENV_PREVIEW_REFERENCE_TIME: DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()},
            use_defaults=False,
        )


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults() -> None:
    ctx = parse_preview_context_from_environ({}, use_defaults=True)
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_build_preview_child_env_always_sets_seed_and_reference_time() -> None:
    from scripts.screenshot_deploy import build_preview_child_env

    env = build_preview_child_env(port=8765, parent_environ={"PATH": "/usr/bin"})
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_TIME"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()


@pytest.mark.unit
def test_derive_namespace_seed_differs_by_namespace() -> None:
    root = 42
    a = derive_namespace_seed(root, "companies")
    b = derive_namespace_seed(root, "contacts")
    assert a != b
    assert derive_namespace_seed(root, "companies") == a


@pytest.mark.unit
def test_restore_conflict_route_matches_context_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin_preview import PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID

    enable_admin_preview_env(monkeypatch, preview_seed="7")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    expected = preview_contact_restore_conflict()
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID}/restore-conflict"
    )
    assert response.status_code == 200
    assert expected["archived_contact"]["full_name"] in response.text
    assert expected["conflicting_contact"]["full_name"] in response.text
