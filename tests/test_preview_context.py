"""Deterministic preview fixture context tests (#338)."""

from __future__ import annotations

import multiprocessing
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    build_preview_pipeline_companies,
    build_preview_section_rows,
)
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_ROOT_SEED,
    ENV_PREVIEW_FIXTURE_VERSION,
    ENV_PREVIEW_REFERENCE_TIME,
    ENV_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    derive_fixture_seed,
    parse_preview_reference_time,
    parse_preview_root_seed,
)
from app.main import app
from tests.conftest import enable_admin_preview_env


def _ctx(seed: int = 42) -> PreviewContext:
    return PreviewContext.from_values(seed, DEFAULT_PREVIEW_REFERENCE_TIME)


def _serialize_acquisition(data: object) -> dict[str, object]:
    from dataclasses import asdict

    return asdict(data)  # type: ignore[arg-type]


@pytest.mark.unit
def test_same_context_and_route_deeply_equal_across_calls() -> None:
    ctx = _ctx(42)
    now = ctx.reference_time
    a = build_preview_acquisition_dashboard_data(
        rng=ctx.rng_for("acquisition_dashboard"),
        now=now,
    )
    b = build_preview_acquisition_dashboard_data(
        rng=ctx.rng_for("acquisition_dashboard"),
        now=now,
    )
    assert _serialize_acquisition(a) == _serialize_acquisition(b)


@pytest.mark.unit
def test_same_route_identical_when_other_routes_requested_between() -> None:
    ctx = _ctx(99)
    now = ctx.reference_time
    first = build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=now,
    )
    build_preview_contacts(rng=ctx.rng_for("contacts"), now=now)
    build_preview_section_rows(
        "/admin/signals",
        rng=ctx.rng_for("section_rows:/admin/signals"),
        now=now,
    )
    build_preview_pipeline_companies(
        rng=ctx.rng_for("pipeline_companies"),
        now=now,
    )
    second = build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=now,
    )
    assert first == second


@pytest.mark.unit
def test_desktop_and_mobile_http_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv(ENV_PREVIEW_SEED, "42")
    monkeypatch.setenv(
        ENV_PREVIEW_REFERENCE_TIME,
        DEFAULT_PREVIEW_REFERENCE_TIME.isoformat(),
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    headers = {"User-Agent": "desktop-agent"}
    mobile_headers = {"User-Agent": "mobile-agent"}
    desktop = client.get(
        "/admin/companies",
        headers=headers,
        cookies={"admin_session": "preview-screenshot-session"},
    )
    mobile = client.get(
        "/admin/companies",
        headers=mobile_headers,
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


def _worker_companies(payload: tuple[int, str]) -> list[dict[str, object]]:
    seed, iso_time = payload
    ctx = PreviewContext.from_values(
        seed,
        datetime.fromisoformat(iso_time),
    )
    return build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=ctx.reference_time,
    )


@pytest.mark.unit
def test_multiple_worker_processes_produce_identical_fixtures() -> None:
    payload = (77, DEFAULT_PREVIEW_REFERENCE_TIME.isoformat())
    with multiprocessing.Pool(processes=2) as pool:
        results = pool.map(_worker_companies, [payload, payload])
    assert results[0] == results[1]


@pytest.mark.unit
def test_namespace_change_affects_only_that_fixture() -> None:
    ctx = _ctx(55)
    now = ctx.reference_time
    companies_before = build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=now,
    )
    contacts_before = build_preview_contacts(
        rng=ctx.rng_for("contacts"),
        now=now,
    )
    # Simulate adding an unrelated fixture namespace (would run in another route).
    build_preview_section_rows(
        "/admin/analytics",
        rng=ctx.rng_for("section_rows:/admin/analytics"),
        now=now,
    )
    companies_after = build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=now,
    )
    contacts_after = build_preview_contacts(
        rng=ctx.rng_for("contacts"),
        now=now,
    )
    assert companies_before == companies_after
    assert contacts_before == contacts_after


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset() -> None:
    now = DEFAULT_PREVIEW_REFERENCE_TIME
    a = build_preview_brief_rows(
        rng=_ctx(1).rng_for("brief_rows"),
        now=now,
    )
    b = build_preview_brief_rows(
        rng=_ctx(1).rng_for("brief_rows"),
        now=now,
    )
    c = build_preview_brief_rows(
        rng=_ctx(2).rng_for("brief_rows"),
        now=now,
    )
    assert a == b
    assert a != c


@pytest.mark.unit
def test_frozen_time_controls_overdue_upcoming_and_date_boundaries() -> None:
    frozen = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
    ctx = PreviewContext.from_values(42, frozen)
    data = build_preview_acquisition_dashboard_data(
        rng=ctx.rng_for("acquisition_dashboard"),
        now=ctx.reference_time,
    )
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at is not None
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at is not None
        assert row.next_action_due_at > frozen
    for row in data.stale_evidence:
        assert row.expires_at is not None
        assert row.expires_at < frozen
    for row in data.recent_evidence:
        assert row.expires_at is not None
        assert row.expires_at > frozen

    companies = build_preview_companies(
        rng=ctx.rng_for("companies"),
        now=ctx.reference_time,
    )
    for row in companies:
        verified = row.get("last_verified_at")
        archived = row.get("archived_at")
        if verified:
            verified_date = datetime.fromisoformat(str(verified)).date()
            assert verified_date <= (frozen + timedelta(days=45)).date()
        if archived:
            archived_dt = datetime.fromisoformat(str(archived))
            assert archived_dt < frozen


@pytest.mark.unit
def test_preview_builders_do_not_call_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PREVIEW_SEED, "42")
    monkeypatch.setenv(
        ENV_PREVIEW_REFERENCE_TIME,
        DEFAULT_PREVIEW_REFERENCE_TIME.isoformat(),
    )
    with patch("app.admin_preview.datetime") as mocked_dt:
        mocked_dt.now.side_effect = AssertionError("datetime.now must not be called")
        mocked_dt.side_effect = datetime
        data = build_preview_acquisition_dashboard_data()
        assert data.generated_at == DEFAULT_PREVIEW_REFERENCE_TIME


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    from screenshot_deploy import (
        PRE_BRANCH_PHASE,
        build_screenshot_reproducibility_record,
        format_reproducibility_markdown,
    )

    record = build_screenshot_reproducibility_record(
        phase=PRE_BRANCH_PHASE,
        head_sha="abc123",
        browser_version="120.0.6099.0",
        preview_context=_ctx(DEFAULT_PREVIEW_ROOT_SEED),
    )
    assert record["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert record["preview_root_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert record["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert record["head_sha"] == "abc123"
    assert record["browser_version"] == "120.0.6099.0"
    assert record["viewports"]
    assert record["admin_extra_viewports"]
    md = format_reproducibility_markdown(record)
    assert any("preview_root_seed" in line for line in md)
    assert any("338001" in line for line in md)


@pytest.mark.unit
def test_malformed_fixture_version_fails_fast() -> None:
    with pytest.raises(PreviewContextError):
        PreviewContext.from_environ({ENV_PREVIEW_FIXTURE_VERSION: "0"})


@pytest.mark.unit
def test_malformed_reference_time_fails_fast() -> None:
    with pytest.raises(PreviewContextError):
        parse_preview_reference_time("2026-07-14T12:00:00")


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults() -> None:
    ctx = PreviewContext.from_environ({})
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_build_preview_child_env_always_sets_seed_and_reference_time() -> None:
    from screenshot_deploy import build_preview_child_env

    env = build_preview_child_env(port=8765, parent_environ={"PATH": "/usr/bin"})
    assert env[ENV_PREVIEW_SEED] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env[ENV_PREVIEW_REFERENCE_TIME] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert env["ADMIN_PREVIEW_FIXTURE_VERSION"] == str(PREVIEW_FIXTURE_VERSION)


@pytest.mark.unit
def test_derive_fixture_seed_is_stable_and_namespace_specific() -> None:
    a = derive_fixture_seed(42, "companies")
    b = derive_fixture_seed(42, "companies")
    c = derive_fixture_seed(42, "contacts")
    assert a == b
    assert a != c
    assert random.Random(a).random() != random.Random(c).random()
