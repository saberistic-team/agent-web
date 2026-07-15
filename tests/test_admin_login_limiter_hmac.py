"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, db
from app.admin_auth import LoginAdmissionResult
from app.admin_security import validate_admin_login_limiter_secret, validate_admin_security_settings
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    _login,
    client,
    mock_db_connection,
    shared_rate_limiter,
)

TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"

_ADMITTED = LoginAdmissionResult(
    admitted=True,
    throttled=False,
    already_locked=False,
    lockout_transition=False,
)


@contextmanager
def _admitted_login_attempt() -> Generator[None, None, None]:
    with patch(
        "app.admin_routes.admin_auth.try_admit_login_attempt",
        return_value=_ADMITTED,
    ):
        yield

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    fields = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "plausible_domain": base.plausible_domain,
        "plausible_api_key": base.plausible_api_key,
        "analytics_environment": base.analytics_environment,
        "admin_username": base.admin_username,
        "admin_password_hash": base.admin_password_hash,
        "admin_session_secret": base.admin_session_secret,
        "admin_login_limiter_secret": base.admin_login_limiter_secret,
        "admin_login_limiter_secret_previous": base.admin_login_limiter_secret_previous,
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    keyed = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = admin_auth.plain_sha256_limiter_key("src", source)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings = _settings()
    source = "203.0.113.10"
    current = admin_auth.build_source_rate_limit_key(source, settings=settings)
    rotated = admin_auth.build_source_rate_limit_key(
        source,
        secret=TEST_LIMITER_SECRET_ALT,
    )
    assert current != rotated


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_calls() -> None:
    settings = _settings()
    source = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(source, settings=settings)
    second = admin_auth.build_source_rate_limit_key(source, settings=settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_source_and_account() -> None:
    settings = _settings()
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
def test_limiter_secret_validation_rejects_placeholder_value() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        validate_admin_login_limiter_secret("placeholder-placeholder-placehold!")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "required"),
        ("short-secret", "at least 32 bytes"),
        ("changeme", "at least 32 bytes"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "too weak"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_settings(settings)


@pytest.mark.unit
def test_rotation_lookup_includes_previous_secret_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    write_keys = admin_auth.login_limiter_write_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    lookup_keys = admin_auth.login_limiter_lookup_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(write_keys) == 2
    assert len(lookup_keys) == 4
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.10",
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    assert previous_source in lookup_keys
    assert previous_source not in write_keys


@pytest.mark.unit
def test_try_admit_honors_guard_keys_without_incrementing() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    cur.fetchall.return_value = [
        {
            "limiter_key": "guard-only",
            "failure_count": 5,
            "window_started_at": now - timedelta(minutes=1),
            "locked_until": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        },
        {
            "limiter_key": "write-key",
            "failure_count": 0,
            "window_started_at": now,
            "locked_until": None,
        },
    ]

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=("write-key",),
        guard_keys=("guard-only",),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    executed_sql = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "UPDATE admin_login_rate_limits" not in executed_sql
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor_only() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    with mock_db_connection(), _admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.audit_service.get_repositories",
                return_value=MagicMock(audit_events=repo),
            ):
                candidate = "ghost-candidate@example.com"
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
                repo.append.assert_called_once()
                assert repo.append.call_args.kwargs["actor"] == "anonymous"
                payload = json.dumps(repo.append.call_args.kwargs)
                assert candidate not in payload
                assert "ghost" not in payload.lower()


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    with mock_db_connection(), _admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.audit_service.get_repositories",
                return_value=MagicMock(audit_events=repo),
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
                assert repo.append.call_args.kwargs["actor"] == "anonymous"
                assert TEST_USERNAME not in json.dumps(repo.append.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_failure_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    with mock_db_connection(), _admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.audit_service.get_repositories",
                return_value=MagicMock(audit_events=repo),
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "flow-attacker",
                        "password": TEST_PASSWORD,
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 400
                assert repo.append.call_args.kwargs["actor"] == "anonymous"
                assert repo.append.call_args.kwargs["summary_after"]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_records_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    store = FakeRateLimitStore()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    with shared_rate_limiter(store):
        with mock_db_connection():
            with patch(
                "app.audit_service.get_repositories",
                return_value=MagicMock(audit_events=repo),
            ):
                assert _login(password="wrong").status_code == 401
                lockout = _login(password="wrong")
                assert lockout.status_code == 401
                last_call = repo.append.call_args_list[-1]
                assert last_call.kwargs["actor"] == "anonymous"
                assert last_call.kwargs["summary_after"]["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with mock_db_connection(), _admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 303
                    actor_context = success_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == TEST_USERNAME
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_exclude_candidate_and_secret(caplog: pytest.LogCaptureFixture) -> None:
    candidate = "log-candidate-user"
    secret = TEST_LIMITER_SECRET
    caplog.set_level(logging.INFO)
    with mock_db_connection(), _admitted_login_attempt():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.audit_service.record_login_failure",
                side_effect=RuntimeError("audit down"),
            ):
                client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    combined = f"{caplog.text}\n{''.join(str(record.exc_text or '') for record in caplog.records)}"
    for forbidden in (candidate, secret, "src:", "acct:", "203.0.113"):
        assert forbidden not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@contextmanager
def _pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def pg_limiter_conn() -> Generator[psycopg.Connection, None, None]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
    with _pg_conn(database_url) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
                cleanup.execute("CREATE SCHEMA public")
                cleanup.commit()


@pytest.mark.integration
def test_pg_persists_keyed_limiter_identifiers_and_anonymous_actor(
    pg_limiter_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = admin_auth.plain_sha256_limiter_key("src", source)
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)

    admission = db.try_admit_admin_login(
        pg_limiter_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted
    pg_limiter_conn.commit()

    with pg_limiter_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain

    repo = MagicMock()
    repo.append.side_effect = lambda **_kwargs: {
        "id": "evt",
        "actor": _kwargs["actor_context"].actor,
        "summary_after": _kwargs["summary_after"],
    }

    class _ConnCtx:
        def __enter__(self) -> psycopg.Connection:
            return pg_limiter_conn

        def __exit__(self, *_args: object) -> None:
            return None

    with patch("app.admin_routes.db.db_connection", return_value=_ConnCtx()):
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.audit_service.get_repositories",
                return_value=MagicMock(audit_events=repo),
            ):
                candidate = "pg-candidate-user"
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401

    actor_context = repo.append.call_args.kwargs
    assert actor_context["actor"] == "anonymous"
    assert candidate not in json.dumps(repo.append.call_args.kwargs)
    pg_limiter_conn.rollback()


@pytest.mark.integration
def test_rotation_window_blocks_on_previous_key_and_cleans_expired_rows(
    pg_limiter_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.90",
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    locked_until = now + timedelta(seconds=900)
    with pg_limiter_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (previous_source, 5, now - timedelta(minutes=5), locked_until, now),
        )
    pg_limiter_conn.commit()

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    write_keys = admin_auth.login_limiter_write_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.90",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    lookup_keys = admin_auth.login_limiter_lookup_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.90",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    guard_keys = tuple(key for key in lookup_keys if key not in write_keys)

    blocked = db.try_admit_admin_login(
        pg_limiter_conn,
        limiter_keys=write_keys,
        guard_keys=guard_keys,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked

    expired_now = locked_until + timedelta(seconds=1)
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_limiter_conn,
        now=expired_now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted >= 1
    pg_limiter_conn.rollback()


@pytest.mark.integration
def test_pg_concurrent_admission_with_hmac_keys(
    pg_limiter_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.91", settings=settings)
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()
    database_url = _require_database_url()

    def worker() -> None:
        barrier.wait()
        with _pg_conn(database_url) as conn:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            if admission.admitted:
                with lock:
                    admitted_count["value"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert admitted_count["value"] == 5
    pg_limiter_conn.rollback()
