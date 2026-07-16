"""Deterministic ADMIN_PREVIEW_MODE context and namespace RNG tests."""

from __future__ import annotations

import multiprocessing as mp
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    build_preview_pipeline_companies,
)
from app.main import app
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewConfigError,
    derive_namespace_seed,
    get_preview_context,
    parse_preview_context_from_env,
    preview_now,
    preview_rng,
    reset_preview_context_cache,
)


@pytest.fixture(autouse=True)
def _clear_preview_context_cache() -> None:
    reset_preview_context_cache()
    yield
    reset_preview_context_cache()


@pytest.mark.unit
def test_same_context_and_route_produces_deeply_equal_fixtures() -> None:
    ctx = parse_preview_context_from_env(seed_raw="42", reference_raw="2026-07-15T12:00:00+00:00")
    rng = preview_rng("fixture:companies", context=ctx)
    now = preview_now(context=ctx)
    a = build_preview_companies(rng=rng, now=now)
    b = build_preview_companies(rng=preview_rng("fixture:companies", context=ctx), now=now)
    assert a == b


@pytest.mark.unit
def test_same_route_identical_when_other_routes_requested_first() -> None:
    ctx = parse_preview_context_from_env(seed_raw="99", reference_raw="2026-07-15T12:00:00+00:00")
    now = preview_now(context=ctx)
    _ = build_preview_acquisition_dashboard_data(
        rng=preview_rng("fixture:acquisition_dashboard", context=ctx),
        now=now,
    )
    _ = build_preview_brief_rows(
        rng=preview_rng("fixture:brief_rows", context=ctx),
        now=now,
    )
    first = build_preview_pipeline_companies(
        rng=preview_rng("fixture:pipeline_companies", context=ctx),
        now=now,
    )
    _ = build_preview_contacts(
        rng=preview_rng("fixture:contacts", context=ctx),
        now=now,
    )
    second = build_preview_pipeline_companies(
        rng=preview_rng("fixture:pipeline_companies", context=ctx),
        now=now,
    )
    assert first == second


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_and_mobile_http_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T12:00:00+00:00")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    desktop = client.get("/admin/companies")
    mobile = client.get(
        "/admin/companies",
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
    )
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


def _worker_companies(payload: tuple[int, str]) -> list[dict[str, object]]:
    seed, reference = payload
    import os

    os.environ["ADMIN_PREVIEW_SEED"] = str(seed)
    os.environ["ADMIN_PREVIEW_REFERENCE_TIME"] = reference
    reset_preview_context_cache()
    return build_preview_companies()


@pytest.mark.unit
def test_multiple_worker_processes_produce_identical_fixtures() -> None:
    payload = (338, "2026-07-15T12:00:00+00:00")
    with mp.Pool(2) as pool:
        results = pool.map(_worker_companies, [payload, payload])
    assert results[0] == results[1]


@pytest.mark.unit
def test_namespace_change_affects_only_that_fixture() -> None:
    ctx = parse_preview_context_from_env(seed_raw="5", reference_raw="2026-07-15T12:00:00+00:00")
    now = preview_now(context=ctx)
    companies_a = build_preview_companies(
        rng=preview_rng("fixture:companies", context=ctx),
        now=now,
    )
    companies_b = build_preview_companies(
        rng=preview_rng("fixture:companies", context=ctx),
        now=now,
    )
    contacts = build_preview_contacts(
        rng=preview_rng("fixture:contacts", context=ctx),
        now=now,
    )
    assert companies_a == companies_b
    assert contacts[0] != companies_a  # different fixture namespaces


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset() -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    ctx_a = parse_preview_context_from_env(seed_raw="1", reference_raw=now.isoformat())
    ctx_b = parse_preview_context_from_env(seed_raw="2", reference_raw=now.isoformat())
    a = build_preview_brief_rows(
        rng=preview_rng("fixture:brief_rows", context=ctx_a),
        now=now,
    )
    b = build_preview_brief_rows(
        rng=preview_rng("fixture:brief_rows", context=ctx_b),
        now=now,
    )
    c = build_preview_brief_rows(
        rng=preview_rng("fixture:brief_rows", context=ctx_a),
        now=now,
    )
    assert a == c
    assert a != b


@pytest.mark.unit
def test_frozen_time_controls_overdue_upcoming_and_date_boundaries() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    ctx = parse_preview_context_from_env(seed_raw="42", reference_raw=frozen.isoformat())
    data = build_preview_acquisition_dashboard_data(
        rng=preview_rng("fixture:acquisition_dashboard", context=ctx),
        now=frozen,
    )
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen
    for row in data.stale_evidence:
        assert row.expires_at < frozen
    for row in data.recent_evidence:
        assert row.expires_at > frozen


@pytest.mark.unit
def test_preview_builders_do_not_call_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T12:00:00+00:00")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("datetime.now must not be called when preview context is set")

    monkeypatch.setattr("app.admin_preview.datetime", type("X", (), {"now": staticmethod(_boom)}))
    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == preview_now()


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from screenshot_deploy import build_preview_manifest

    manifest = build_preview_manifest(
        head_sha="abc123",
        browser_version="120.0",
    )
    assert manifest["fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["root_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert manifest["reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert manifest["head_sha"] == "abc123"
    assert manifest["browser_version"] == "120.0"
    assert manifest["viewports"]


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults() -> None:
    ctx = parse_preview_context_from_env(seed_raw="", reference_raw="")
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME


@pytest.mark.unit
def test_malformed_seed_or_timestamp_fail_fast() -> None:
    with pytest.raises(PreviewConfigError):
        parse_preview_context_from_env(seed_raw="not-valid-🎲", reference_raw="")
    with pytest.raises(PreviewConfigError):
        parse_preview_context_from_env(seed_raw="", reference_raw="not-a-date")
    with pytest.raises(PreviewConfigError):
        parse_preview_context_from_env(seed_raw="", reference_raw="2026-07-15T12:00:00")


@pytest.mark.unit
def test_derive_namespace_seed_is_stable_and_versioned() -> None:
    a = derive_namespace_seed(42, "fixture:companies", fixture_version=1)
    b = derive_namespace_seed(42, "fixture:companies", fixture_version=1)
    c = derive_namespace_seed(42, "fixture:contacts", fixture_version=1)
    d = derive_namespace_seed(42, "fixture:companies", fixture_version=2)
    assert a == b
    assert a != c
    assert a != d
    rng1 = preview_rng("fixture:companies", context=get_preview_context())
    rng2 = preview_rng("fixture:companies", context=get_preview_context())
    assert [rng1.random() for _ in range(5)] == [rng2.random() for _ in range(5)]


@pytest.mark.unit
def test_explicit_rng_override_still_supported() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_companies(rng=random.Random(42), now=now)
    b = build_preview_companies(rng=random.Random(42), now=now)
    assert a == b
