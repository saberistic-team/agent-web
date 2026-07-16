"""Deterministic ADMIN_PREVIEW_MODE fixture contract (issue #338)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    DEFAULT_PREVIEW_REFERENCE_TIME,
    DEFAULT_PREVIEW_ROOT_SEED,
    NS_ACQUISITION_DASHBOARD,
    NS_BRIEF_ROWS,
    NS_COMPANIES,
    NS_CONTACTS,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_contacts,
    derive_namespace_seed,
    load_preview_context_from_env,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_context_manifest_fields,
    preview_now,
    preview_rng_for,
)
from app.main import app


def _preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: str | None = None,
    reference_time: str | None = None,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    if seed is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    if reference_time is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", reference_time)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)


@pytest.mark.unit
def test_same_context_and_route_deeply_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="42", reference_time="2026-07-15T12:00:00+00:00")
    a = build_preview_companies()
    b = build_preview_companies()
    assert a == b
    dashboard_a = build_preview_acquisition_dashboard_data()
    dashboard_b = build_preview_acquisition_dashboard_data()
    assert dashboard_a == dashboard_b


@pytest.mark.unit
def test_route_identical_when_other_fixtures_requested_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    baseline = build_preview_brief_rows()
    _ = build_preview_contacts()
    _ = build_preview_acquisition_dashboard_data()
    after = build_preview_brief_rows()
    assert baseline == after


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_and_mobile_http_responses_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="42", reference_time="2026-07-15T12:00:00+00:00")
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


@pytest.mark.unit
def test_multiple_worker_processes_identical_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="99", reference_time="2026-07-15T12:00:00+00:00")
    script = """
import json
import os
os.environ["ADMIN_PREVIEW_MODE"] = "1"
os.environ["ADMIN_PREVIEW_SEED"] = "99"
os.environ["ADMIN_PREVIEW_REFERENCE_TIME"] = "2026-07-15T12:00:00+00:00"
from app.admin_preview import build_preview_companies
print(json.dumps(build_preview_companies(), default=str))
"""
    repo_root = os.getcwd()
    outputs: list[str] = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONPATH": repo_root},
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


@pytest.mark.unit
def test_namespace_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="42")
    companies_before = build_preview_companies()
    contacts_before, _ = build_preview_contacts()
    _ = build_preview_acquisition_dashboard_data()
    companies_after = build_preview_companies()
    contacts_after, _ = build_preview_contacts()
    assert companies_before == companies_after
    assert contacts_before == contacts_after
    assert derive_namespace_seed(42, NS_COMPANIES, PREVIEW_FIXTURE_VERSION) != (
        derive_namespace_seed(42, NS_CONTACTS, PREVIEW_FIXTURE_VERSION)
    )


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="1", reference_time="2026-07-15T12:00:00+00:00")
    seed_one_a = build_preview_brief_rows()
    seed_one_b = build_preview_brief_rows()
    assert seed_one_a == seed_one_b

    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "2")
    seed_two_a = build_preview_brief_rows()
    seed_two_b = build_preview_brief_rows()
    assert seed_two_a == seed_two_b
    assert seed_one_a != seed_two_a


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_freshness_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = "2026-07-15T12:00:00+00:00"
    _preview_env(monkeypatch, seed="42", reference_time=frozen)
    dashboard = build_preview_acquisition_dashboard_data()
    assert dashboard.generated_at == parse_preview_reference_time(frozen)
    assert all(
        row.next_action_due_at < dashboard.generated_at
        for row in dashboard.overdue_actions
    )
    assert all(
        row.next_action_due_at > dashboard.generated_at
        for row in dashboard.upcoming_actions
    )
    companies = build_preview_companies()
    assert companies
    for row in companies:
        verified = row.get("last_verified_at")
        if verified:
            assert isinstance(verified, str)


@pytest.mark.unit
def test_preview_builders_skip_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = "2026-07-15T12:00:00+00:00"
    _preview_env(monkeypatch, seed="42", reference_time=frozen)
    reference = parse_preview_reference_time(frozen)
    companies = build_preview_companies()
    briefs = build_preview_brief_rows()
    dashboard = build_preview_acquisition_dashboard_data()
    assert preview_now() == reference
    assert dashboard.generated_at == reference
    for row in briefs:
        created = row["created_at"]
        assert isinstance(created, datetime)
        assert created.tzinfo is not None
        assert created <= reference
    for row in companies:
        archived = row.get("archived_at")
        if archived:
            assert str(archived) <= reference.isoformat()


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screenshot_deploy import (
        build_screenshot_repro_manifest,
        comment_markdown_pre_dual,
        format_repro_manifest_comment_lines,
    )

    _preview_env(monkeypatch, seed="42", reference_time="2026-07-15T12:00:00+00:00")
    monkeypatch.setenv("GITHUB_SHA", "abc123def456")
    manifest = build_screenshot_repro_manifest(browser_version="120.0.0")
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["preview_root_seed"] == 42
    assert manifest["preview_reference_time"] == "2026-07-15T12:00:00+00:00"
    assert manifest["git_head_sha"] == "abc123def456"
    assert manifest["browser_version"] == "120.0.0"
    assert manifest["viewports"] == []

    ctx = PreviewContext(
        root_seed=42,
        reference_time=DEFAULT_PREVIEW_REFERENCE_TIME,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    fields = preview_context_manifest_fields(
        ctx,
        head_sha="sha",
        browser_version="1.2.3",
        viewports=[{"name": "desktop", "width": 1, "height": 2}],
    )
    comment_lines = format_repro_manifest_comment_lines(fields)
    assert any("fixture_version" in line for line in comment_lines)
    assert any("root_seed" in line for line in comment_lines)

    body = comment_markdown_pre_dual(
        branch_url="http://127.0.0.1:8765",
        branch_urls=["https://raw.example/branch-home.png"],
        repro_manifest=manifest,
    )
    assert "preview reproducibility" in body
    assert "`42`" in body


@pytest.mark.unit
def test_screenshot_manifest_file_written(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from screenshot_deploy import write_screenshot_repro_manifest

    _preview_env(monkeypatch)
    path = write_screenshot_repro_manifest(
        tmp_path, phase="branch", browser_version="120.0.0"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["preview_root_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert payload["preview_reference_time"] == DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()
    assert payload["browser_version"] == "120.0.0"


@pytest.mark.unit
def test_malformed_seed_or_time_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "not-a-number")
    with pytest.raises(ValueError, match="ADMIN_PREVIEW_SEED"):
        load_preview_context_from_env()

    _preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "yesterday")
    with pytest.raises(ValueError, match="ADMIN_PREVIEW_REFERENCE_TIME"):
        load_preview_context_from_env()


@pytest.mark.unit
def test_missing_seed_and_time_use_stable_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    ctx = load_preview_context_from_env()
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_time == DEFAULT_PREVIEW_REFERENCE_TIME
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_parse_preview_seed_and_time_helpers() -> None:
    assert parse_preview_seed("42") == 42
    with pytest.raises(ValueError):
        parse_preview_seed("abc")
    parsed = parse_preview_reference_time("2026-07-15T12:00:00+00:00")
    assert parsed.tzinfo == timezone.utc
    with pytest.raises(ValueError):
        parse_preview_reference_time("2026-07-15T12:00:00")


@pytest.mark.unit
def test_preview_rng_for_namespace_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="42")
    a = preview_rng_for(NS_BRIEF_ROWS)
    b = preview_rng_for(NS_BRIEF_ROWS)
    assert [a.random(), a.random(), a.random()] == [b.random(), b.random(), b.random()]
    assert derive_namespace_seed(42, NS_BRIEF_ROWS, PREVIEW_FIXTURE_VERSION) != (
        derive_namespace_seed(42, NS_COMPANIES, PREVIEW_FIXTURE_VERSION)
    )
