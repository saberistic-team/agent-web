"""Tests for existence-independent admin login candidate throttling (#241)."""

from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.rows import dict_row

from app import admin_auth, db
from tests.test_admin_auth import (
    LOGIN_FLOW_COOKIE_NAME,
    TEST_CF_EDGE,
    TEST_PASSWORD,
    TEST_RENDER_PEER,
    TEST_TRUSTED_EDGE_CIDRS,
    TEST_TRUSTED_PROXY_CIDRS,
    TEST_USERNAME,
    FakeRateLimitStore,
    _login,
    _login_flow_set_cookie_headers,
    mock_db_connection,
    shared_rate_limiter,
)
from tests.test_admin_auth import client

from app.config import get_settings
from app.migrations.runner import apply_migrations

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-login-limiter-secret-32chars-min")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "900")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "900")
    monkeypatch.delenv("ADMIN_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_CIDRS", raising=False)
    admin_auth.reset_login_rate_limiter()
    client.cookies.clear()


def _normalize_set_cookie_headers(headers: list[str]) -> list[str]:
    normalized: list[str] = []
    for header in headers:
        if LOGIN_FLOW_COOKIE_NAME in header:
            normalized.append(
                re.sub(
                    rf"{LOGIN_FLOW_COOKIE_NAME}=[^;]+",
                    f"{LOGIN_FLOW_COOKIE_NAME}=<normalized>",
                    header,
                )
            )
        else:
            normalized.append(header)
    return normalized


@dataclass
class _LoginStepObservation:
    status_code: int
    body: str
    set_cookie_headers: list[str]
    verify_calls: int
    durable_row_count: int
    durable_writes: int


@dataclass
class _SequenceTrace:
    steps: list[_LoginStepObservation] = field(default_factory=list)


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres enumeration tests")


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    with _connect(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                _reset_public_schema(cleanup)


def _trusted_source_headers(source_index: int) -> dict[str, str]:
    return {
        "X-Test-Client-Host": TEST_RENDER_PEER,
        "X-Forwarded-For": f"203.0.113.{source_index}, {TEST_CF_EDGE}",
    }


def _run_cross_source_sequence(
    *,
    rate_limit_store: FakeRateLimitStore,
    username: str,
    rate_limit: int,
    source_count: int | None = None,
) -> _SequenceTrace:
    if source_count is None:
        source_count = rate_limit

    trace = _SequenceTrace()
    verify_calls = {"count": 0}
    durable_writes = {"count": 0}
    original_verify = admin_auth.verify_admin_credentials

    def counting_verify(candidate: str, password: str, settings: Any) -> bool:
        verify_calls["count"] += 1
        return original_verify(candidate, password, settings)

    original_try_admit = rate_limit_store.try_admit

    def counting_try_admit(*args: Any, **kwargs: Any) -> db.AdminLoginAdmission:
        durable_writes["count"] += 1
        return original_try_admit(*args, **kwargs)

    rate_limit_store.try_admit = counting_try_admit  # type: ignore[method-assign]

    with shared_rate_limiter(rate_limit_store):
        with (
            mock_db_connection(),
            patch(
                "app.admin_routes.admin_auth.verify_admin_credentials",
                side_effect=counting_verify,
            ),
        ):
            for index in range(source_count):
                response = _login(
                    username=username,
                    password="wrong",
                    headers=_trusted_source_headers(index + 1),
                )
                trace.steps.append(
                    _LoginStepObservation(
                        status_code=response.status_code,
                        body=response.text,
                        set_cookie_headers=_login_flow_set_cookie_headers(response),
                        verify_calls=verify_calls["count"],
                        durable_row_count=len(rate_limit_store.rows),
                        durable_writes=durable_writes["count"],
                    )
                )

            overflow = _login(
                username=username,
                password="wrong",
                headers=_trusted_source_headers(source_count + 1),
            )
            trace.steps.append(
                _LoginStepObservation(
                    status_code=overflow.status_code,
                    body=overflow.text,
                    set_cookie_headers=_login_flow_set_cookie_headers(overflow),
                    verify_calls=verify_calls["count"],
                    durable_row_count=len(rate_limit_store.rows),
                    durable_writes=durable_writes["count"],
                )
            )

    return trace


def _normalize_login_body(body: str) -> str:
    return re.sub(
        r'name="csrf_token" value="[^"]+"',
        'name="csrf_token" value="<normalized>"',
        body,
    )


def _assert_equivalent_traces(left: _SequenceTrace, right: _SequenceTrace) -> None:
    assert len(left.steps) == len(right.steps)
    for left_step, right_step in zip(left.steps, right.steps):
        assert left_step.status_code == right_step.status_code
        assert _normalize_login_body(left_step.body) == _normalize_login_body(right_step.body)
        assert _normalize_set_cookie_headers(left_step.set_cookie_headers) == _normalize_set_cookie_headers(
            right_step.set_cookie_headers
        )
        assert left_step.verify_calls == right_step.verify_calls
        assert left_step.durable_writes == right_step.durable_writes


@pytest.mark.unit
@pytest.mark.integration
def test_cross_source_existing_candidate_reaches_candidate_threshold(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    trace = _run_cross_source_sequence(
        rate_limit_store=rate_limit_store,
        username=TEST_USERNAME,
        rate_limit=2,
    )
    assert [step.status_code for step in trace.steps] == [401, 401, 429]
    assert trace.steps[-1].verify_calls == 2
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in trace.steps[-1].body
    assert trace.steps[-1].set_cookie_headers == []


@pytest.mark.unit
@pytest.mark.integration
def test_cross_source_unknown_candidate_reaches_candidate_threshold(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    trace = _run_cross_source_sequence(
        rate_limit_store=rate_limit_store,
        username="ghost-candidate",
        rate_limit=2,
    )
    assert [step.status_code for step in trace.steps] == [401, 401, 429]
    assert trace.steps[-1].verify_calls == 2
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in trace.steps[-1].body
    assert trace.steps[-1].set_cookie_headers == []


@pytest.mark.unit
@pytest.mark.integration
def test_existing_and_unknown_candidates_produce_equivalent_sequences(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    existing = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username=TEST_USERNAME,
        rate_limit=2,
    )
    unknown = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username="ghost-candidate",
        rate_limit=2,
    )
    _assert_equivalent_traces(existing, unknown)


@pytest.mark.unit
@pytest.mark.integration
def test_several_unknown_candidates_share_policy_without_existence_leak(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    traces = [
        _run_cross_source_sequence(
            rate_limit_store=FakeRateLimitStore(),
            username=f"ghost-{index}",
            rate_limit=2,
        )
        for index in range(3)
    ]
    configured = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username=TEST_USERNAME,
        rate_limit=2,
    )
    for trace in traces:
        _assert_equivalent_traces(trace, configured)


@pytest.mark.unit
@pytest.mark.integration
def test_normalized_spellings_share_candidate_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    first = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username=" Operator ",
        rate_limit=2,
    )
    second = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username="operator",
        rate_limit=2,
    )
    _assert_equivalent_traces(first, second)


@pytest.mark.integration
def test_concurrent_distributed_admission_matches_for_existing_and_unknown(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rate_limit = 5
    existing_key = admin_auth.build_candidate_rate_limit_key(TEST_USERNAME, settings)
    unknown_key = admin_auth.build_candidate_rate_limit_key("ghost-candidate", settings)
    barrier = threading.Barrier(8)
    existing_admitted = {"value": 0}
    unknown_admitted = {"value": 0}
    lock = threading.Lock()

    def worker(candidate_key: str, counter: dict[str, int], thread_index: int) -> None:
        barrier.wait()
        with _connect(_DATABASE_URL) as conn:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(
                    admin_auth.build_source_rate_limit_key(
                        f"203.0.113.{thread_index + 1}",
                        settings,
                    ),
                    candidate_key,
                ),
                now=now,
                rate_limit=rate_limit,
                window_seconds=900,
                lockout_seconds=900,
            )
            if admission.admitted:
                with lock:
                    counter["value"] += 1

    existing_threads = [
        threading.Thread(target=worker, args=(existing_key, existing_admitted, index))
        for index in range(8)
    ]
    unknown_threads = [
        threading.Thread(target=worker, args=(unknown_key, unknown_admitted, index))
        for index in range(8)
    ]
    for thread in (*existing_threads, *unknown_threads):
        thread.start()
    for thread in (*existing_threads, *unknown_threads):
        thread.join()

    assert existing_admitted["value"] == rate_limit
    assert unknown_admitted["value"] == rate_limit


@pytest.mark.unit
@pytest.mark.integration
def test_window_boundary_equivalence_for_existing_and_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "60")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    settings = get_settings()
    start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def seed_locked_rows(store: FakeRateLimitStore, username: str) -> None:
        source_key = admin_auth.build_source_rate_limit_key(TEST_RENDER_PEER, settings)
        candidate_key = admin_auth.build_candidate_rate_limit_key(username, settings)
        lockout_end = start + timedelta(seconds=60)
        for row_key in (source_key, candidate_key):
            store.rows[row_key] = {
                "failure_count": 2,
                "window_started_at": start,
                "locked_until": lockout_end,
                "updated_at": start,
            }

    def observe(username: str, now: datetime) -> int:
        store = FakeRateLimitStore()
        seed_locked_rows(store, username)
        with shared_rate_limiter(store):
            with (
                mock_db_connection(),
                patch(
                    "app.admin_auth.datetime",
                    wraps=datetime,
                ) as mock_datetime,
            ):
                mock_datetime.now.return_value = now
                response = _login(
                    username=username,
                    password="wrong",
                    headers=_trusted_source_headers(1),
                )
        return response.status_code

    before_lockout_end = start + timedelta(seconds=29)
    after_lockout_end = start + timedelta(seconds=61)
    after_window = start + timedelta(seconds=121)

    assert observe(TEST_USERNAME, before_lockout_end) == observe("ghost-candidate", before_lockout_end) == 429
    assert observe(TEST_USERNAME, after_lockout_end) == observe("ghost-candidate", after_lockout_end) == 401
    assert observe(TEST_USERNAME, after_window) == observe("ghost-candidate", after_window) == 401


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_clears_only_submitted_candidate_bucket(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    configured_candidate = admin_auth.build_candidate_rate_limit_key(TEST_USERNAME, settings)
    other_candidate = admin_auth.build_candidate_rate_limit_key("ghost-candidate", settings)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            assert _login(username="ghost-candidate", password="wrong").status_code == 401
            assert other_candidate in rate_limit_store.rows

            recovery = _login()
            assert recovery.status_code == 303
            assert configured_candidate not in rate_limit_store.rows
            assert other_candidate in rate_limit_store.rows
            assert source_key in rate_limit_store.rows


@pytest.mark.integration
def test_candidate_storage_is_bounded_and_cleanup_removes_stale_rows(
    pg_conn: psycopg.Connection,
) -> None:
    settings = get_settings()
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    unique_candidates = 25

    for index in range(unique_candidates):
        candidate_key = admin_auth.build_candidate_rate_limit_key(
            f"candidate-{index}",
            settings,
        )
        source_key = admin_auth.build_source_rate_limit_key(f"203.0.113.{index}", settings)
        db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(source_key, candidate_key),
            now=now,
            rate_limit=5,
            window_seconds=60,
            lockout_seconds=60,
        )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == unique_candidates * 2

    deleted_total = 0
    while True:
        deleted = db.cleanup_expired_admin_login_rate_limits(
            pg_conn,
            now=now + timedelta(seconds=200),
            window_seconds=60,
            lockout_seconds=60,
            batch_size=admin_auth.LOGIN_RATE_LIMIT_CLEANUP_BATCH_SIZE,
        )
        deleted_total += deleted
        if deleted == 0:
            break
    assert deleted_total == unique_candidates * 2

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.unit
@pytest.mark.integration
def test_store_failure_path_matches_for_existing_and_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")

    def run_candidate(username: str) -> tuple[int, str]:
        admin_auth.reset_login_rate_limiter()
        client.cookies.clear()
        with (
            patch(
                "app.admin_auth.db.try_admit_admin_login",
                side_effect=Exception("database unavailable"),
            ),
            patch(
                "app.admin_auth.db.cleanup_expired_admin_login_rate_limits",
                side_effect=Exception("database unavailable"),
            ),
            mock_db_connection(),
        ):
            for _ in range(2):
                response = _login(username=username, password="wrong")
                assert response.status_code == 401
            blocked = _login(username=username, password="wrong")
            return blocked.status_code, blocked.text

    existing_status, existing_body = run_candidate(TEST_USERNAME)
    unknown_status, unknown_body = run_candidate("ghost-candidate")
    assert existing_status == unknown_status == 429
    assert _normalize_login_body(existing_body) == _normalize_login_body(unknown_body)
    assert admin_auth.LOGIN_THROTTLED_MESSAGE in existing_body


@pytest.mark.unit
@pytest.mark.integration
def test_admission_work_count_independent_of_candidate_existence(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", TEST_TRUSTED_PROXY_CIDRS)
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", TEST_TRUSTED_EDGE_CIDRS)

    existing = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username=TEST_USERNAME,
        rate_limit=2,
        source_count=1,
    )
    unknown = _run_cross_source_sequence(
        rate_limit_store=FakeRateLimitStore(),
        username="ghost-candidate",
        rate_limit=2,
        source_count=1,
    )
    assert existing.steps[0].verify_calls == unknown.steps[0].verify_calls == 1
    assert existing.steps[0].durable_writes == unknown.steps[0].durable_writes == 1
    assert existing.steps[0].durable_row_count == unknown.steps[0].durable_row_count == 2
