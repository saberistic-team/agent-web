"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE (#338)."""

from __future__ import annotations

import json
import multiprocessing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_section_rows,
)
from app.main import app
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME_ISO,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    derive_fixture_seed,
    fixture_now,
    fixture_rng,
    load_preview_context,
    preview_reproducibility_metadata,
)


@pytest.mark.unit
def test_same_context_and_route_deeply_equal() -> None:
    ctx = PreviewContext(
        root_seed=42,
        reference_now=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        fixture_version="1",
    )
    rng = fixture_rng("companies", context=ctx)
    now = fixture_now(context=ctx)
    a = build_preview_companies(rng=rng, now=now)
    b = build_preview_companies(rng=fixture_rng("companies", context=ctx), now=now)
    assert a == b


@pytest.mark.unit
def test_route_fixture_independent_of_other_route_order() -> None:
    ctx = PreviewContext(
        root_seed=99,
        reference_now=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        fixture_version="1",
    )
    # Simulate other routes being built first (different namespaces).
    _ = build_preview_section_rows(
        "/admin/signals",
        rng=fixture_rng("section_rows:/admin/signals", context=ctx),
        now=fixture_now(context=ctx),
    )
    _ = build_preview_brief_rows(
        rng=fixture_rng("brief_rows", context=ctx),
        now=fixture_now(context=ctx),
    )
    companies_first = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    _ = build_preview_acquisition_dashboard_data(
        rng=fixture_rng("acquisition_dashboard", context=ctx),
        now=fixture_now(context=ctx),
    )
    companies_second = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    assert companies_first == companies_second


@pytest.mark.unit
def test_desktop_and_mobile_http_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    desktop = client.get(
        "/admin/companies",
        headers={"User-Agent": "desktop-capture"},
        cookies={"admin_session": "preview-screenshot-session"},
    )
    mobile = client.get(
        "/admin/companies",
        headers={"User-Agent": "mobile-capture"},
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


def _worker_build_companies(
    root_seed: int,
    reference_iso: str,
    queue: multiprocessing.Queue,
) -> None:
    ctx = PreviewContext(
        root_seed=root_seed,
        reference_now=datetime.fromisoformat(reference_iso),
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    rows = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    queue.put(rows)


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures() -> None:
    reference_iso = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    queue: multiprocessing.Queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_worker_build_companies,
            args=(DEFAULT_PREVIEW_ROOT_SEED, reference_iso, queue),
        )
        for _ in range(3)
    ]
    for proc in processes:
        proc.start()
    results = [queue.get(timeout=30) for _ in processes]
    for proc in processes:
        proc.join(timeout=30)
    assert results[0] == results[1] == results[2]


@pytest.mark.unit
def test_namespace_change_affects_only_that_fixture() -> None:
    ctx = PreviewContext(
        root_seed=7,
        reference_now=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        fixture_version="1",
    )
    companies_before = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    signals_before = build_preview_section_rows(
        "/admin/signals",
        rng=fixture_rng("section_rows:/admin/signals", context=ctx),
        now=fixture_now(context=ctx),
    )
    _ = build_preview_brief_rows(
        rng=fixture_rng("brief_rows", context=ctx),
        now=fixture_now(context=ctx),
    )
    companies_after = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    signals_after = build_preview_section_rows(
        "/admin/signals",
        rng=fixture_rng("section_rows:/admin/signals", context=ctx),
        now=fixture_now(context=ctx),
    )
    assert companies_before == companies_after
    assert signals_before == signals_after
    companies_only = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    brief_rows = build_preview_brief_rows(
        rng=fixture_rng("brief_rows", context=ctx),
        now=fixture_now(context=ctx),
    )
    assert companies_only != brief_rows


def build_preview_contacts_from_ctx(ctx: PreviewContext) -> list[dict[str, object]]:
    from app.admin_preview import build_preview_contacts

    contacts, _companies = build_preview_contacts(
        rng=fixture_rng("contacts", context=ctx),
        now=fixture_now(context=ctx),
    )
    return contacts


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    ctx_a = PreviewContext(root_seed=1, reference_now=now, fixture_version="1")
    ctx_b = PreviewContext(root_seed=2, reference_now=now, fixture_version="1")
    a = build_preview_brief_rows(rng=fixture_rng("brief_rows", context=ctx_a), now=now)
    b = build_preview_brief_rows(rng=fixture_rng("brief_rows", context=ctx_b), now=now)
    c = build_preview_brief_rows(rng=fixture_rng("brief_rows", context=ctx_a), now=now)
    assert a != b
    assert a == c


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_freshness_boundaries() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(root_seed=42, reference_now=now, fixture_version="1")
    dashboard = build_preview_acquisition_dashboard_data(
        rng=fixture_rng("acquisition_dashboard", context=ctx),
        now=fixture_now(context=ctx),
    )
    assert dashboard.generated_at == now
    assert all(row.next_action_due_at < now for row in dashboard.overdue_actions)
    assert all(row.next_action_due_at > now for row in dashboard.upcoming_actions)
    companies = build_preview_companies(
        rng=fixture_rng("companies", context=ctx),
        now=fixture_now(context=ctx),
    )
    for row in companies:
        archived_at = row.get("archived_at")
        if archived_at:
            archived_dt = datetime.fromisoformat(str(archived_at))
            assert archived_dt < now
        verified = row.get("last_verified_at")
        if verified:
            verified_date = datetime.fromisoformat(str(verified)).date()
            assert verified_date <= (now + timedelta(days=45)).date()


@pytest.mark.unit
def test_preview_builders_use_frozen_reference_time_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = "2026-01-15T08:30:00+00:00"
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen)
    first = build_preview_acquisition_dashboard_data()
    second = build_preview_acquisition_dashboard_data()
    assert first == second
    assert first.generated_at == datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc)


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    metadata = preview_reproducibility_metadata(
        head_sha="abc123",
        browser_version="120.0.0.0",
        viewports=[{"name": "desktop", "width": 1280, "height": 800}],
        context=PreviewContext(
            root_seed=DEFAULT_PREVIEW_ROOT_SEED,
            reference_now=datetime.fromisoformat(DEFAULT_PREVIEW_REFERENCE_TIME_ISO),
            fixture_version=PREVIEW_FIXTURE_VERSION,
        ),
    )
    assert metadata["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert metadata["preview_root_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert metadata["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    assert metadata["head_sha"] == "abc123"
    assert metadata["browser_version"] == "120.0.0.0"
    assert metadata["viewports"]


@pytest.mark.unit
def test_preview_server_env_sets_seed_and_reference_time() -> None:
    from screenshot_deploy import preview_server_env

    env = preview_server_env(base_url="http://127.0.0.1:8765")
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_TIME"] == DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    assert env["ADMIN_PREVIEW_FIXTURE_VERSION"] == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_malformed_reference_time_fails_fast() -> None:
    with pytest.raises(PreviewContextError):
        load_preview_context(seed="42", reference_time="July 14 2026 noon")


@pytest.mark.unit
def test_missing_values_use_documented_defaults() -> None:
    ctx = load_preview_context(seed=None, reference_time=None, fixture_version=None)
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_now == datetime.fromisoformat(DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_strict_mode_requires_explicit_values() -> None:
    with pytest.raises(PreviewContextError):
        load_preview_context(seed=None, reference_time=None, strict=True)


@pytest.mark.unit
def test_derive_fixture_seed_documented_construction() -> None:
    seed_a = derive_fixture_seed(root_seed=1, namespace="companies", fixture_version="1")
    seed_b = derive_fixture_seed(root_seed=1, namespace="contacts", fixture_version="1")
    seed_c = derive_fixture_seed(root_seed=1, namespace="companies", fixture_version="1")
    assert seed_a == seed_c
    assert seed_a != seed_b


@pytest.mark.unit
def test_write_reproducibility_manifest_round_trip(tmp_path) -> None:
    from screenshot_deploy import write_reproducibility_manifest

    metadata = {"preview_root_seed": 42, "preview_fixture_version": "1"}
    path = write_reproducibility_manifest(tmp_path, phase="branch", metadata=metadata)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == metadata
