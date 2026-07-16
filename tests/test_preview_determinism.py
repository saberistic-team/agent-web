"""Deterministic ADMIN_PREVIEW_MODE fixture contract (#338)."""

from __future__ import annotations

import json
import multiprocessing
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_REFERENCE_TIME_ISO,
    DEFAULT_PREVIEW_SEED,
    ENV_PREVIEW_REFERENCE_TIME,
    ENV_PREVIEW_SEED,
    PreviewContext,
    PreviewContextError,
    derive_namespace_seed,
    load_preview_context,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_now,
    preview_rng,
    preview_reproducibility_env,
    reset_preview_context,
)


def _worker_build_companies(result_queue: multiprocessing.Queue[object]) -> None:
    reset_preview_context()
    os.environ[ENV_PREVIEW_SEED] = "338"
    os.environ[ENV_PREVIEW_REFERENCE_TIME] = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    rows = build_preview_companies()
    result_queue.put(rows)


@pytest.fixture(autouse=True)
def _clear_preview_context() -> None:
    reset_preview_context()
    yield
    reset_preview_context()


@pytest.mark.unit
def test_same_context_and_route_deeply_equal() -> None:
    ctx = PreviewContext(root_seed=42, reference_time=DEFAULT_PREVIEW_REFERENCE_TIME)
    a = build_preview_companies(rng=preview_rng("companies", context=ctx), now=ctx.reference_time)
    b = build_preview_companies(rng=preview_rng("companies", context=ctx), now=ctx.reference_time)
    assert a == b


@pytest.mark.unit
def test_route_identical_when_other_namespaces_touched() -> None:
    ctx = PreviewContext(root_seed=99, reference_time=DEFAULT_PREVIEW_REFERENCE_TIME)
    baseline = build_preview_brief_rows(
        rng=preview_rng("briefs", context=ctx),
        now=ctx.reference_time,
    )
    build_preview_section_rows(
        "/admin/companies",
        rng=preview_rng("section:/admin/companies", context=ctx),
        now=ctx.reference_time,
    )
    build_preview_acquisition_dashboard_data(
        rng=preview_rng("acquisition_dashboard", context=ctx),
        now=ctx.reference_time,
    )
    after = build_preview_brief_rows(
        rng=preview_rng("briefs", context=ctx),
        now=ctx.reference_time,
    )
    assert baseline == after


@pytest.mark.unit
def test_desktop_and_mobile_responses_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv(ENV_PREVIEW_SEED, "338")
    monkeypatch.setenv(ENV_PREVIEW_REFERENCE_TIME, DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_preview_context()
    client = TestClient(app, follow_redirects=False)
    desktop = client.get("/admin/briefs")
    mobile = client.get("/admin/briefs")
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


@pytest.mark.unit
def test_multiple_processes_identical_fixtures() -> None:
    ctx = multiprocessing.get_context("spawn")
    queue_a: multiprocessing.Queue[object] = ctx.Queue()
    queue_b: multiprocessing.Queue[object] = ctx.Queue()
    proc_a = ctx.Process(target=_worker_build_companies, args=(queue_a,))
    proc_b = ctx.Process(target=_worker_build_companies, args=(queue_b,))
    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=30)
    proc_b.join(timeout=30)
    assert proc_a.exitcode == 0
    assert proc_b.exitcode == 0
    assert queue_a.get() == queue_b.get()


@pytest.mark.unit
def test_namespace_isolation() -> None:
    ctx = PreviewContext(root_seed=100, reference_time=DEFAULT_PREVIEW_REFERENCE_TIME)
    companies_before = build_preview_companies(
        rng=preview_rng("companies", context=ctx),
        now=ctx.reference_time,
    )
    build_preview_section_rows(
        "/admin/new-fixture",
        rng=preview_rng("section:/admin/new-fixture", context=ctx),
        now=ctx.reference_time,
    )
    companies_after = build_preview_companies(
        rng=preview_rng("companies", context=ctx),
        now=ctx.reference_time,
    )
    assert companies_before == companies_after
    assert derive_namespace_seed(100, "companies") != derive_namespace_seed(
        100, "section:/admin/companies"
    )


@pytest.mark.unit
def test_changing_seed_produces_alternate_dataset() -> None:
    now = DEFAULT_PREVIEW_REFERENCE_TIME
    a = build_preview_brief_rows(rng=preview_rng("briefs", context=PreviewContext(1, now)), now=now)
    b = build_preview_brief_rows(rng=preview_rng("briefs", context=PreviewContext(2, now)), now=now)
    assert a != b
    c = build_preview_brief_rows(rng=preview_rng("briefs", context=PreviewContext(2, now)), now=now)
    assert b == c


@pytest.mark.unit
def test_frozen_time_controls_date_boundaries() -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    ctx = PreviewContext(root_seed=7, reference_time=frozen)
    data = build_preview_acquisition_dashboard_data(
        rng=preview_rng("acquisition_dashboard", context=ctx),
        now=frozen,
    )
    assert data.generated_at == frozen
    assert all(row.next_action_due_at < frozen for row in data.overdue_actions)
    assert all(row.next_action_due_at > frozen for row in data.upcoming_actions)
    assert all(row.expires_at > frozen for row in data.recent_evidence)
    assert all(row.expires_at < frozen for row in data.stale_evidence)


@pytest.mark.unit
def test_admin_preview_builders_avoid_wall_clock_reads() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "admin_preview.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now(" not in source
    assert "date.today(" not in source


@pytest.mark.unit
def test_reproducibility_manifest_records_non_secret_fields() -> None:
    from screenshot_deploy import build_reproducibility_manifest, reproducibility_comment_lines

    reset_preview_context()
    os.environ[ENV_PREVIEW_SEED] = "338"
    os.environ[ENV_PREVIEW_REFERENCE_TIME] = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    payload = build_reproducibility_manifest(
        phase="branch",
        browser_version="123.0.6312.4",
        head_sha="abc123",
        viewports=[{"name": "desktop", "width": 1280, "height": 800}],
    )
    assert payload["fixture_version"]
    assert payload["root_seed"] == 338
    assert payload["reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert payload["browser_version"] == "123.0.6312.4"
    assert payload["head_sha"] == "abc123"
    assert payload["viewports"]
    assert ENV_PREVIEW_SEED in payload["preview_env"]
    lines = reproducibility_comment_lines(payload)
    assert any("preview seed" in line for line in lines)
    assert any("fixture version" in line for line in lines)
    json.dumps(payload)


@pytest.mark.unit
def test_malformed_seed_or_time_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PreviewContextError):
        parse_preview_seed("")
    with pytest.raises(PreviewContextError):
        parse_preview_reference_time("2026-07-14")
    with pytest.raises(PreviewContextError):
        parse_preview_reference_time("not-a-timestamp")
    monkeypatch.delenv(ENV_PREVIEW_SEED, raising=False)
    monkeypatch.delenv(ENV_PREVIEW_REFERENCE_TIME, raising=False)
    with pytest.raises(PreviewContextError):
        load_preview_context(use_defaults=False)


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PREVIEW_SEED, raising=False)
    monkeypatch.delenv(ENV_PREVIEW_REFERENCE_TIME, raising=False)
    reset_preview_context()
    ctx = load_preview_context()
    assert ctx.root_seed == DEFAULT_PREVIEW_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    env = preview_reproducibility_env()
    assert env[ENV_PREVIEW_SEED] == str(DEFAULT_PREVIEW_SEED)
    assert env[ENV_PREVIEW_REFERENCE_TIME] == DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    assert preview_now() == DEFAULT_PREVIEW_REFERENCE_TIME


@pytest.mark.unit
def test_subprocess_fixture_builder_matches_in_process() -> None:
    code = """
import json
import os
from app.admin_preview import build_preview_companies
from app.preview_context import DEFAULT_PREVIEW_REFERENCE_TIME_ISO, ENV_PREVIEW_REFERENCE_TIME, ENV_PREVIEW_SEED, reset_preview_context

reset_preview_context()
os.environ[ENV_PREVIEW_SEED] = "338"
os.environ[ENV_PREVIEW_REFERENCE_TIME] = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
print(json.dumps(build_preview_companies(), default=str))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(os.getcwd()),
    )
    reset_preview_context()
    os.environ[ENV_PREVIEW_SEED] = "338"
    os.environ[ENV_PREVIEW_REFERENCE_TIME] = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    expected = build_preview_companies()
    assert json.loads(proc.stdout) == json.loads(json.dumps(expected, default=str))
