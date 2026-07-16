"""Tests for deterministic ADMIN_PREVIEW_MODE fixture context (#338)."""

from __future__ import annotations

import multiprocessing as mp
import random
from datetime import datetime, timezone
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
    build_preview_acquisition_dashboard_data,
    build_preview_brief_rows,
    build_preview_companies,
    build_preview_section_rows,
    preview_contact_restore_conflict,
)
from app.preview_context import (
    DEFAULT_PREVIEW_REFERENCE_ISO,
    DEFAULT_PREVIEW_ROOT_SEED,
    PREVIEW_FIXTURE_VERSION,
    PreviewContext,
    PreviewContextError,
    clear_preview_context_cache,
    derive_fixture_seed,
    fixture_rng,
    load_preview_context,
    parse_preview_reference_time,
    parse_preview_seed,
    preview_reference_now,
)


@pytest.fixture(autouse=True)
def _reset_preview_context_cache() -> None:
    clear_preview_context_cache()
    yield
    clear_preview_context_cache()


@pytest.mark.unit
def test_load_preview_context_uses_stable_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_SEED", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_REFERENCE_TIME", raising=False)
    monkeypatch.delenv("ADMIN_PREVIEW_FIXTURE_VERSION", raising=False)
    ctx = load_preview_context()
    assert ctx.root_seed == DEFAULT_PREVIEW_ROOT_SEED
    assert ctx.reference_now == parse_preview_reference_time(DEFAULT_PREVIEW_REFERENCE_ISO)
    assert ctx.fixture_version == PREVIEW_FIXTURE_VERSION


@pytest.mark.unit
def test_malformed_preview_seed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "not-a-number")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_SEED"):
        load_preview_context(use_defaults=False)


@pytest.mark.unit
def test_malformed_preview_reference_time_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "yesterday")
    with pytest.raises(PreviewContextError, match="ADMIN_PREVIEW_REFERENCE_TIME"):
        load_preview_context()


@pytest.mark.unit
def test_naive_reference_time_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-15T14:30:00")
    with pytest.raises(PreviewContextError, match="timezone-aware"):
        load_preview_context()


@pytest.mark.unit
def test_same_context_and_route_deeply_equal_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-14T12:00:00+00:00")
    a = build_preview_brief_rows()
    b = build_preview_brief_rows()
    assert a == b
    assert len(a) >= 5


@pytest.mark.unit
def test_route_fixture_independent_of_other_route_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "99")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-14T12:00:00+00:00")
    baseline = build_preview_companies()
    build_preview_brief_rows()
    build_preview_section_rows("/admin/signals")
    build_preview_acquisition_dashboard_data()
    after = build_preview_companies()
    assert baseline == after


@pytest.mark.unit
def test_namespace_isolation() -> None:
    ctx = load_preview_context()
    companies_a = build_preview_companies()
    signals_rows = build_preview_section_rows("/admin/signals")
    companies_b = build_preview_companies()
    assert companies_a == companies_b
    assert signals_rows != build_preview_section_rows("/admin/companies")


@pytest.mark.unit
def test_changing_seed_produces_deterministic_alternate_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-14T12:00:00+00:00")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "1")
    clear_preview_context_cache()
    one = build_preview_brief_rows()
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "2")
    clear_preview_context_cache()
    two = build_preview_brief_rows()
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "1")
    clear_preview_context_cache()
    one_again = build_preview_brief_rows()
    assert one == one_again
    assert one != two


@pytest.mark.unit
def test_frozen_time_controls_overdue_and_upcoming_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen.isoformat())
    data = build_preview_acquisition_dashboard_data()
    assert data.generated_at == frozen
    assert data.overdue_actions
    assert data.upcoming_actions
    for row in data.overdue_actions:
        assert row.next_action_due_at < frozen
    for row in data.upcoming_actions:
        assert row.next_action_due_at > frozen


@pytest.mark.unit
def test_preview_builders_do_not_call_wall_clock_when_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-14T12:00:00+00:00")
    with mock.patch("app.admin_preview.datetime") as mocked_dt:
        mocked_dt.now.side_effect = AssertionError("wall-clock now() in preview path")
        mocked_dt.side_effect = datetime
        rows = build_preview_brief_rows()
    assert rows


@pytest.mark.unit
def test_derive_fixture_seed_documented_construction() -> None:
    a = derive_fixture_seed(42, "briefs", fixture_version="1")
    b = derive_fixture_seed(42, "briefs", fixture_version="1")
    c = derive_fixture_seed(42, "companies", fixture_version="1")
    assert a == b
    assert a != c


@pytest.mark.unit
def test_fixture_rng_matches_explicit_seed_construction() -> None:
    ctx = PreviewContext(
        root_seed=7,
        reference_now=datetime(2026, 7, 14, tzinfo=timezone.utc),
        fixture_version="1",
    )
    expected = random.Random(derive_fixture_seed(7, "briefs", fixture_version="1"))
    actual = fixture_rng("briefs", ctx)
    assert [expected.randint(0, 10_000) for _ in range(5)] == [
        actual.randint(0, 10_000) for _ in range(5)
    ]


def _worker_brief_count(seed: str, reference: str) -> int:
    import os

    os.environ["ADMIN_PREVIEW_SEED"] = seed
    os.environ["ADMIN_PREVIEW_REFERENCE_TIME"] = reference
    from app.preview_context import clear_preview_context_cache

    clear_preview_context_cache()
    from app.admin_preview import build_preview_brief_rows

    return len(build_preview_brief_rows())


@pytest.mark.unit
def test_multiple_worker_processes_produce_identical_fixtures() -> None:
    reference = "2026-07-14T12:00:00+00:00"
    with mp.Pool(processes=2) as pool:
        results = pool.starmap(
            _worker_brief_count,
            [("55", reference), ("55", reference)],
        )
    assert results[0] == results[1]


@pytest.mark.unit
@pytest.mark.integration
def test_desktop_and_mobile_responses_share_fixture_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from app.main import app

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-07-14T12:00:00+00:00")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    expected = build_preview_brief_rows()
    first_brief = expected[0]
    client = TestClient(app, follow_redirects=False)
    listing = client.get("/admin/briefs")
    assert listing.status_code == 200
    assert str(first_brief["id"]) in listing.text
    assert str(first_brief["website"]) in listing.text
    detail = client.get("/admin/briefs/1")
    assert detail.status_code == 200
    assert str(first_brief["website"]) in detail.text
    assert "Paid" in detail.text


@pytest.mark.unit
def test_preview_reference_now_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", "2026-06-01T08:15:00+00:00")
    assert preview_reference_now() == datetime(2026, 6, 1, 8, 15, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_preview_seed_requires_value_when_no_default() -> None:
    with pytest.raises(PreviewContextError):
        parse_preview_seed("", default=None)


@pytest.mark.unit
def test_restore_conflict_uses_frozen_reference_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    monkeypatch.setenv("ADMIN_PREVIEW_REFERENCE_TIME", frozen.isoformat())
    payload = preview_contact_restore_conflict()
    archived_at = datetime.fromisoformat(str(payload["archived_contact"]["archived_at"]))
    assert archived_at < frozen
