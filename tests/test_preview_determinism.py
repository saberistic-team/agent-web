"""Deterministic ADMIN_PREVIEW_MODE fixtures for stable screenshot runs (#338)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import (
    DEFAULT_PREVIEW_REFERENCE_AT,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_dashboard_data,
    derive_preview_fixture_seed,
    format_preview_reference_at,
    get_preview_context,
    preview_context_manifest_fields,
    preview_rng_for_namespace,
    reset_preview_context_cache,
)
from app.main import app


@pytest.fixture(autouse=True)
def _clear_preview_context_cache() -> None:
    reset_preview_context_cache()
    yield
    reset_preview_context_cache()


def _preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: str | None = None,
    reference_at: str | None = None,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if seed is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    if reference_at is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_AT", reference_at)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_AT", raising=False)


@pytest.mark.unit
def test_same_context_and_route_deeply_equal() -> None:
    now = DEFAULT_PREVIEW_REFERENCE_AT
    a = build_preview_acquisition_dashboard_data(
        rng=preview_rng_for_namespace("acquisition_dashboard"),
        now=now,
    )
    b = build_preview_acquisition_dashboard_data(
        rng=preview_rng_for_namespace("acquisition_dashboard"),
        now=now,
    )
    assert a == b


@pytest.mark.unit
def test_route_fixture_independent_of_other_route_order() -> None:
    ctx = PreviewContext(root_seed=99, reference_at=DEFAULT_PREVIEW_REFERENCE_AT)
    now = ctx.reference_at

    def companies_once() -> list[dict[str, object]]:
        return build_preview_companies(
            rng=preview_rng_for_namespace("companies"),
            now=now,
        )

    baseline = companies_once()
    build_preview_dashboard_data(
        rng=preview_rng_for_namespace("dashboard"),
        now=now,
    )
    build_preview_brief_rows(
        rng=preview_rng_for_namespace("brief_rows"),
        now=now,
    )
    assert companies_once() == baseline


@pytest.mark.unit
def test_namespace_change_affects_only_that_fixture() -> None:
    ctx = PreviewContext(root_seed=7, reference_at=DEFAULT_PREVIEW_REFERENCE_AT)
    now = ctx.reference_at
    companies_a = build_preview_companies(
        rng=preview_rng_for_namespace("companies"),
        now=now,
    )
    contacts_a = build_preview_contacts_fixture(now=now)
    companies_b = build_preview_companies(
        rng=preview_rng_for_namespace("companies_v2"),
        now=now,
    )
    contacts_b = build_preview_contacts_fixture(now=now)
    assert companies_a != companies_b
    assert contacts_a == contacts_b


def build_preview_contacts_fixture(*, now: datetime) -> tuple[list, list]:
    from app.admin_preview import build_preview_contacts

    return build_preview_contacts(
        rng=preview_rng_for_namespace("contacts"),
        now=now,
    )


@pytest.mark.unit
def test_changing_root_seed_produces_deterministic_alternate_dataset() -> None:
    now = DEFAULT_PREVIEW_REFERENCE_AT
    seed_a = derive_preview_fixture_seed("dashboard", context=PreviewContext(1, now))
    seed_b = derive_preview_fixture_seed("dashboard", context=PreviewContext(2, now))
    data_a = build_preview_dashboard_data(rng=random.Random(seed_a), now=now)
    data_b = build_preview_dashboard_data(rng=random.Random(seed_b), now=now)
    data_a_repeat = build_preview_dashboard_data(rng=random.Random(seed_a), now=now)
    assert data_a == data_a_repeat
    assert data_a != data_b


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_freshness_boundaries() -> None:
    frozen = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    rng = random.Random(123)
    dashboard = build_preview_acquisition_dashboard_data(rng=rng, now=frozen)
    assert dashboard.generated_at == frozen
    assert all(row.next_action_due_at < frozen for row in dashboard.overdue_actions)
    assert all(row.next_action_due_at > frozen for row in dashboard.upcoming_actions)
    assert all(row.expires_at > frozen for row in dashboard.recent_evidence)
    assert all(row.expires_at < frozen for row in dashboard.stale_evidence)

    companies = build_preview_companies(rng=rng, now=frozen)
    fresh = [row for row in companies if row.get("last_verified_at")]
    assert fresh
    for row in fresh:
        verified = datetime.fromisoformat(str(row["last_verified_at"])).date()
        assert (frozen.date() - verified).days <= 90


@pytest.mark.unit
def test_missing_ci_values_use_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    ctx = get_preview_context(reload=True)
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_at == DEFAULT_PREVIEW_REFERENCE_AT
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_malformed_seed_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="not-a-number")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        get_preview_context(reload=True)


@pytest.mark.unit
def test_malformed_reference_at_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, reference_at="yesterday")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_AT"):
        get_preview_context(reload=True)


@pytest.mark.unit
def test_naive_reference_at_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, reference_at="2026-07-15T12:00:00")
    with pytest.raises(PreviewContextError, match="timezone-aware"):
        get_preview_context(reload=True)


@pytest.mark.unit
def test_preview_builders_use_frozen_context_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    _preview_env(monkeypatch, seed="55", reference_at=format_preview_reference_at(frozen))
    data = build_preview_dashboard_data()
    assert data.generated_at.startswith("2020-01-02")


@pytest.mark.unit
def test_subprocess_workers_produce_identical_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _preview_env(
        monkeypatch,
        seed="808",
        reference_at=format_preview_reference_at(DEFAULT_PREVIEW_REFERENCE_AT),
    )
    script = tmp_path / "worker.py"
    script.write_text(
        """
import json
import os
import random
from app.admin_preview import (
    build_preview_brief_rows,
    derive_preview_fixture_seed,
    get_preview_context,
    preview_rng_for_namespace,
)

os.environ.setdefault("ADMIN_PREVIEW_MODE", "1")
ctx = get_preview_context(reload=True)
rng = preview_rng_for_namespace("brief_rows")
rows = build_preview_brief_rows(rng=rng, now=ctx.reference_at)
print(json.dumps([row["id"] for row in rows]))
""",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    outputs = [
        subprocess.check_output([sys.executable, str(script)], env=env, text=True).strip()
        for _ in range(3)
    ]
    assert len(set(outputs)) == 1


@pytest.mark.integration
def test_desktop_and_mobile_responses_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    frozen = DEFAULT_PREVIEW_REFERENCE_AT
    _preview_env(
        monkeypatch,
        seed="42",
        reference_at=format_preview_reference_at(frozen),
    )
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
    client = TestClient(app, follow_redirects=False)
    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}

    for path in ("/admin/briefs", "/admin/companies", "/admin/contacts"):
        first = client.get(path, cookies=cookies)
        second = client.get(path, cookies=cookies)
        assert first.status_code == 200
        assert first.text == second.text


@pytest.mark.unit
def test_screenshot_reproducibility_manifest_records_fields() -> None:
    from screenshot_deploy import (
        build_screenshot_reproducibility_manifest,
        format_reproducibility_manifest_lines,
    )

    manifest = build_screenshot_reproducibility_manifest(
        browser_version="chromium-120.0",
        head_sha="abc123",
    )
    assert manifest["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert manifest["preview_root_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert manifest["preview_reference_at"] == format_preview_reference_at(
        DEFAULT_PREVIEW_REFERENCE_AT
    )
    assert manifest["preview_head_sha"] == "abc123"
    assert manifest["browser"] == "chromium-120.0"
    assert manifest["viewports"]
    lines = format_reproducibility_manifest_lines(manifest)
    assert any("preview_root_seed" in line for line in lines)
    assert any("preview_reference_at" in line for line in lines)


@pytest.mark.unit
def test_preview_context_manifest_fields_no_secrets() -> None:
    fields = preview_context_manifest_fields(head_sha="deadbeef")
    assert "preview_root_seed" in fields
    assert "preview_reference_at" in fields
    assert "password" not in json.dumps(fields).lower()
    assert "secret" not in json.dumps(fields).lower()


@pytest.mark.unit
def test_preview_server_env_sets_stable_defaults() -> None:
    from screenshot_deploy import _preview_server_env

    env = _preview_server_env()
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_AT"] == format_preview_reference_at(
        DEFAULT_PREVIEW_REFERENCE_AT
    )


@pytest.mark.unit
def test_derive_preview_fixture_seed_stable_across_processes() -> None:
    ctx = PreviewContext(root_seed=338, reference_at=DEFAULT_PREVIEW_REFERENCE_AT)
    a = derive_preview_fixture_seed("brief_rows", context=ctx)
    b = derive_preview_fixture_seed("brief_rows", context=ctx)
    assert a == b
    assert derive_preview_fixture_seed("companies", context=ctx) != a


@pytest.mark.unit
def test_fixture_version_bump_changes_derived_seed() -> None:
    ref = DEFAULT_PREVIEW_REFERENCE_AT
    ctx_v1 = PreviewContext(root_seed=1, reference_at=ref, fixture_version=1)
    ctx_v2 = PreviewContext(root_seed=1, reference_at=ref, fixture_version=2)
    assert derive_preview_fixture_seed("dashboard", context=ctx_v1) != (
        derive_preview_fixture_seed("dashboard", context=ctx_v2)
    )
