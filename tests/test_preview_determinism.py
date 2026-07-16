"""Deterministic ADMIN_PREVIEW_MODE fixture context (issue #338)."""

from __future__ import annotations

import multiprocessing
import random
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    build_preview_dashboard_data,
    build_preview_pipeline_companies,
    build_preview_section_rows,
    clear_preview_context_cache,
    derive_namespace_seed,
    get_preview_context,
    load_preview_context_from_env,
    preview_rng_for,
)
from app.main import app


def _preview_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from argon2 import PasswordHasher

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", str(DEFAULT_PREVIEW_SEED))
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        DEFAULT_PREVIEW_REFERENCE_TIME.isoformat(),
    )
    monkeypatch.setenv("ADMIN_PREVIEW_FIXTURE_VERSION", PREVIEW_FIXTURE_VERSION)
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_SECRET",
        "preview-limiter-secret-32chars-minimum!!",
    )
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    clear_preview_context_cache()
    return TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def _reset_preview_context_cache() -> None:
    clear_preview_context_cache()
    yield
    clear_preview_context_cache()


@pytest.mark.unit
def test_same_context_route_deep_equal_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        "2026-07-14T12:00:00+00:00",
    )
    a = build_preview_companies()
    b = build_preview_companies()
    assert a == b
    assert len(a) >= 4


@pytest.mark.unit
def test_route_stable_when_other_routes_requested_between(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "99")
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        "2026-07-14T12:00:00+00:00",
    )
    baseline = build_preview_brief_rows()
    build_preview_acquisition_dashboard_data()
    build_preview_section_rows("/admin/signals")
    build_preview_contacts()
    again = build_preview_brief_rows()
    assert baseline == again


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_mobile_http_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _preview_client(monkeypatch)
    cookies = {"admin_session": "preview-screenshot-session"}
    first = client.get("/admin/briefs", cookies=cookies)
    second = client.get("/admin/briefs", cookies=cookies)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text
    assert "brief-row-link" in first.text
    assert "#1" in first.text


def _worker_build_companies(
    seed: int,
    reference_iso: str,
    queue: multiprocessing.Queue,
) -> None:
    import os

    os.environ["ADMIN_PREVIEW_SEED"] = str(seed)
    os.environ["ADMIN_PREVIEW_REFERENCE_TIME"] = reference_iso
    clear_preview_context_cache()
    queue.put(build_preview_pipeline_companies())


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures() -> None:
    reference = DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    queue: multiprocessing.Queue = multiprocessing.Queue()
    workers = [
        multiprocessing.Process(
            target=_worker_build_companies,
            args=(DEFAULT_PREVIEW_SEED, reference, queue),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
    assert results[0] == results[1]


@pytest.mark.unit
def test_namespace_isolation_unrelated_fixture_does_not_perturb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "55")
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        "2026-07-14T12:00:00+00:00",
    )
    companies_before = build_preview_companies()
    build_preview_section_rows("/admin/analytics")
    companies_after = build_preview_companies()
    assert companies_before == companies_after
    ctx = get_preview_context()
    assert preview_rng_for("companies", ctx) != preview_rng_for("section:/admin/analytics", ctx)


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        "2026-07-14T12:00:00+00:00",
    )
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "1")
    clear_preview_context_cache()
    seed_one_a = build_preview_dashboard_data()
    seed_one_b = build_preview_dashboard_data()
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "2")
    clear_preview_context_cache()
    seed_two = build_preview_dashboard_data()
    assert seed_one_a == seed_one_b
    assert seed_one_a != seed_two


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_date_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen.isoformat())
    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    assert all(row.next_action_due_at < frozen for row in data.overdue_actions)
    assert all(row.next_action_due_at > frozen for row in data.upcoming_actions)
    pipeline = build_preview_pipeline_companies()
    for row in pipeline:
        due = row["next_action_due_at"]
        assert isinstance(due, datetime)
        assert due.tzinfo is not None
        assert abs((due - frozen).days) <= 12


@pytest.mark.unit
def test_no_wall_clock_when_context_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "12")
    monkeypatch.setenv(
        "ADMIN_PREVIEW_REFERENCE_TIME",
        "2026-06-01T08:30:00+00:00",
    )

    with patch("app.admin_preview.datetime") as mock_dt:
        mock_dt.now.side_effect = AssertionError("wall-clock datetime.now called")
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        rows = build_preview_brief_rows()
    assert rows
    assert rows[0]["created_at"].tzinfo is not None


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields() -> None:
    from screenshot_deploy import (
        build_screenshot_reproducibility_manifest,
        comment_markdown_pre_dual,
        format_reproducibility_manifest_lines,
    )

    manifest = build_screenshot_reproducibility_manifest(
        head_sha="abc123",
        browser_version="120.0.0.0",
        viewports=[{"name": "desktop", "width": 1280, "height": 800}],
    )
    assert manifest["admin_preview_seed"] == str(DEFAULT_PREVIEW_SEED)
    assert manifest["admin_preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["head_sha"] == "abc123"
    assert manifest["browser_version"] == "120.0.0.0"
    lines = format_reproducibility_manifest_lines(manifest)
    assert any("admin_preview_seed" in line for line in lines)
    body = comment_markdown_pre_dual(
        branch_url="http://127.0.0.1:8765",
        branch_urls=["https://raw.example/branch-admin.png"],
        reproducibility_manifest=manifest,
    )
    assert "admin_preview_seed" in body
    assert "admin_preview_reference_time" in body


@pytest.mark.unit
def test_malformed_seed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "not-a-number")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        load_preview_context_from_env()


@pytest.mark.unit
def test_malformed_reference_time_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "yesterday")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_TIME"):
        load_preview_context_from_env()


@pytest.mark.unit
def test_missing_env_uses_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    ctx = load_preview_context_from_env()
    assert ctx.root_seed == DEFAULT_PREVIEW_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_derive_namespace_seed_is_stable() -> None:
    a = derive_namespace_seed(338, "companies")
    b = derive_namespace_seed(338, "companies")
    c = derive_namespace_seed(338, "contacts")
    assert a == b
    assert a != c


@pytest.mark.unit
def test_preview_context_from_explicit_values() -> None:
    ctx = PreviewContext(
        root_seed=99,
        reference_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        fixture_version="2",
    )
    assert ctx.reproducibility_fields()["preview_fixture_version"] == "2"
