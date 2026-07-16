"""Deterministic preview fixture context (#338)."""

from __future__ import annotations

import json
import multiprocessing
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_section_rows,
)
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_AT,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    derive_fixture_seed,
    load_preview_context,
    parse_reference_at,
    parse_root_seed,
    preview_now,
    preview_rng_for,
    reset_preview_context_cache,
)
from app.main import app


def _preview_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: str | None = None,
    reference_at: str | None = None,
    preview_mode: str = "1",
) -> None:
    reset_preview_context_cache()
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", preview_mode)
    if seed is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_SEED", seed)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    if reference_at is not None:
        monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_AT", reference_at)
    else:
        monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_AT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")


@pytest.mark.unit
def test_default_context_values() -> None:
    ctx = PreviewContext(
        root_seed=DEFAULT_PREVIEW_ROOT_SEED,
        reference_at=DEFAULT_PREVIEW_REFERENCE_AT,
        fixture_version=PREVIEW_FIXTURE_VERSION,
    )
    assert ctx.root_seed == 338001
    assert ctx.reference_at == DEFAULT_PREVIEW_REFERENCE_AT
    assert ctx.fixture_version == "1"


@pytest.mark.unit
def test_missing_env_uses_stable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_AT", raising=False)
    reset_preview_context_cache()
    ctx = load_preview_context()
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_at == DEFAULT_PREVIEW_REFERENCE_AT


@pytest.mark.unit
def test_malformed_seed_fails_fast() -> None:
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        parse_root_seed("not-a-number")


@pytest.mark.unit
def test_malformed_reference_at_fails_fast() -> None:
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_AT"):
        parse_reference_at("yesterday-ish")


@pytest.mark.unit
def test_same_namespace_produces_identical_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="42")
    a = build_preview_companies()
    b = build_preview_companies()
    assert a == b


@pytest.mark.unit
def test_order_independent_route_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    companies_first = build_preview_companies()
    _ = build_preview_brief_rows()
    _ = build_preview_section_rows("/admin/signals")
    companies_second = build_preview_companies()
    assert companies_first == companies_second


@pytest.mark.unit
def test_namespace_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    companies = build_preview_companies()
    signals = build_preview_section_rows("/admin/signals")
    companies_again = build_preview_companies()
    assert companies == companies_again
    assert signals


@pytest.mark.unit
def test_changing_seed_changes_dataset_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="1")
    first = build_preview_companies()
    reset_preview_context_cache()
    _preview_env(monkeypatch, seed="2")
    second = build_preview_companies()
    reset_preview_context_cache()
    _preview_env(monkeypatch, seed="1")
    repeat = build_preview_companies()
    assert first != second
    assert first == repeat


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    _preview_env(monkeypatch, reference_at=frozen.isoformat())
    from app.admin_preview import build_preview_acquisition_dashboard_data

    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen


@pytest.mark.unit
def test_preview_builders_do_not_call_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    with patch("app.admin_preview.datetime") as mocked:
        mocked.now.side_effect = AssertionError("wall-clock now() in preview path")
        mocked.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        build_preview_companies()
        build_preview_brief_rows()


@pytest.mark.unit
def test_derive_fixture_seed_is_stable() -> None:
    a = derive_fixture_seed(99, "fixture.companies.list")
    b = derive_fixture_seed(99, "fixture.companies.list")
    c = derive_fixture_seed(99, "fixture.brief.rows")
    assert a == b
    assert a != c


@pytest.mark.unit
def test_preview_rng_for_matches_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch, seed="77")
    ctx = load_preview_context()
    assert preview_rng_for("fixture.test").random() == ctx.rng_for("fixture.test").random()


def _worker_build_companies(
    seed: int, reference_iso: str, queue: multiprocessing.Queue[object]
) -> None:
    import os

    os.environ["ADMIN_PREVIEW_MODE"] = "1"
    os.environ["ADMIN_PREVIEW_SEED"] = str(seed)
    os.environ["ADMIN_PREVIEW_REFERENCE_AT"] = reference_iso
    from app.preview_context import reset_preview_context_cache

    reset_preview_context_cache()
    from app.admin_preview import build_preview_companies

    queue.put(build_preview_companies())


@pytest.mark.unit
def test_multiprocess_fixtures_match() -> None:
    reference_iso = DEFAULT_PREVIEW_REFERENCE_AT.isoformat()
    queue: multiprocessing.Queue[object] = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_worker_build_companies,
            args=(DEFAULT_PREVIEW_ROOT_SEED, reference_iso, queue),
        )
        for _ in range(2)
    ]
    for proc in processes:
        proc.start()
    for proc in processes:
        proc.join()
        assert proc.exitcode == 0
    first = queue.get()
    second = queue.get()
    assert first == second


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_and_mobile_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch, seed="42")
    client = TestClient(app, follow_redirects=False)
    desktop = client.get(
        "/admin/companies",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    mobile = client.get(
        "/admin/companies",
        headers={"User-Agent": "mobile"},
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert desktop.status_code == 200
    assert mobile.status_code == 200
    assert desktop.text == mobile.text


@pytest.mark.unit
def test_screenshot_manifest_records_reproducibility_fields(tmp_path) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from screenshot_deploy import write_preview_reproducibility_manifest

    path = write_preview_reproducibility_manifest(
        tmp_path,
        phase="branch",
        head_sha="abc123",
        browser_version="120.0",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["preview_seed"] == DEFAULT_PREVIEW_ROOT_SEED
    assert payload["preview_reference_at"] == DEFAULT_PREVIEW_REFERENCE_AT.isoformat()
    assert payload["preview_fixture_version"] == PREVIEW_FIXTURE_VERSION
    assert payload["head_sha"] == "abc123"
    assert payload["browser_version"] == "120.0"
    assert payload["viewports"]


@pytest.mark.unit
def test_preview_server_env_sets_seed_and_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from screenshot_deploy import _preview_server_env

    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_AT", raising=False)
    env = _preview_server_env({"BASE_URL": "http://127.0.0.1:8765"})
    assert env["ADMIN_PREVIEW_SEED"] == str(DEFAULT_PREVIEW_ROOT_SEED)
    assert env["ADMIN_PREVIEW_REFERENCE_AT"] == DEFAULT_PREVIEW_REFERENCE_AT.isoformat()
    assert env["ADMIN_PREVIEW_FIXTURE_VERSION"] == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_explicit_reference_at_override(monkeypatch: pytest.MonkeyPatch) -> None:
    override = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    _preview_env(monkeypatch, reference_at=override.isoformat())
    assert preview_now() == override
    rows = build_preview_brief_rows()
    assert rows[0]["created_at"] < override
    assert rows[0]["created_at"] > override - timedelta(days=7)
