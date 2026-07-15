"""Tests for HMAC login limiter identifiers and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests import test_admin_auth as auth_tests
from tests.test_admin_auth import (
    FakeRateLimitStore,
    _fetch_login_form,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET_B = "other-limiter-secret-32chars-minimum!!"
TEST_SOURCE = "203.0.113.42"
TEST_PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_secret_previous: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_secret_previous,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
    )


@pytest.fixture(autouse=True)
def reset_auth_test_state() -> None:
    auth_tests.client.cookies.clear()
    auth_tests._login_flows.clear()
    auth_tests._session_store.clear()
    admin_auth.reset_login_rate_limiter()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _plain_sha256_identifier(prefix: str, material: str) -> str:
    return hashlib.sha256(f"{prefix}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert source_key != _plain_sha256_identifier("src", TEST_SOURCE.lower())
    assert account_key != _plain_sha256_identifier("acct", TEST_USERNAME.lower())
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_LIMITER_SECRET_B)
    key_a = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_identical_payloads() -> None:
    settings = _settings()
    shared_material = "operator"
    source_key = admin_auth.digest_limiter_key("src", shared_material, TEST_LIMITER_SECRET)
    account_key = admin_auth.digest_limiter_key("acct", shared_material, TEST_LIMITER_SECRET)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme" + "x" * 24, "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, env_name: str) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_rotation_check_keys_include_previous_secret_variant() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_PREVIOUS_LIMITER_SECRET,
    )
    current_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    check_keys = admin_auth.login_limiter_keys_for_check(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(current_keys) == 2
    assert len(check_keys) == 4
    previous_source = admin_auth.digest_limiter_key(
        "src", TEST_SOURCE.lower(), TEST_PREVIOUS_LIMITER_SECRET
    )
    assert previous_source in check_keys
    assert previous_source not in current_keys


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


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


@pytest.mark.integration
def test_rotation_previous_key_lock_blocks_admission(pg_conn: psycopg.Connection) -> None:
    """Previous-secret rows remain enforceable during the rotation window."""
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_PREVIOUS_LIMITER_SECRET,
    )
    previous_source_key = admin_auth.digest_limiter_key(
        "src", TEST_SOURCE.lower(), TEST_PREVIOUS_LIMITER_SECRET
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(5):
        admission = db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(previous_source_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

    assert db.is_admin_login_throttled(pg_conn, limiter_key=previous_source_key, now=now + timedelta(seconds=10))

    from starlette.requests import Request

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [],
        "client": (TEST_SOURCE, 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = pg_conn
        db_conn.return_value.__exit__.return_value = None
        result = admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=TEST_USERNAME,
        )
    assert not result.admitted
    assert result.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_key_rows(pg_conn: psycopg.Connection) -> None:
    previous_source_key = admin_auth.digest_limiter_key(
        "src", TEST_SOURCE.lower(), TEST_PREVIOUS_LIMITER_SECRET
    )
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_source_key,),
        now=now,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.integration
def test_concurrent_admission_with_hmac_keys_respects_threshold(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key("198.51.100.20", settings)
    now = datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc)
    rate_limit = 5
    barrier = threading.Barrier(8)
    admitted_count = {"value": 0}
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with _connect(_require_database_url()) as conn:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=rate_limit,
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

    assert admitted_count["value"] == rate_limit


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    admin_auth.reset_login_rate_limiter()
    return FakeRateLimitStore()


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(
        conn: MagicMock,
        *,
        actor_context: ActorContext,
        reason: str,
        repository: MagicMock | None = None,
    ) -> dict[str, Any]:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        captured["metadata"] = {"reason": reason}
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    response = _login(username="ghost-attacker", password="wrong-password")
    assert response.status_code == 401
    assert captured["actor"] == "anonymous"
    assert "ghost-attacker" not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(
        conn: MagicMock,
        *,
        actor_context: ActorContext,
        reason: str,
        repository: MagicMock | None = None,
    ) -> dict[str, Any]:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    response = _login(password="wrong-password")
    assert response.status_code == 401
    assert captured["actor"] == "anonymous"
    assert captured["reason"] == "invalid_credentials"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(
        conn: MagicMock,
        *,
        actor_context: ActorContext,
        reason: str,
        repository: MagicMock | None = None,
    ) -> dict[str, Any]:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    csrf_token, cookies = _fetch_login_form()
                    response = auth_tests.client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "tampered-token",
                        },
                        cookies=cookies,
                    )
    assert response.status_code == 400
    assert captured["actor"] == "anonymous"
    assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured: list[str] = []

    def _capture(
        conn: MagicMock,
        *,
        actor_context: ActorContext,
        reason: str,
        repository: MagicMock | None = None,
    ) -> dict[str, Any]:
        captured.append(actor_context.actor)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    assert _login(password="wrong").status_code == 401
                    assert _login(password="wrong").status_code == 401
    assert captured == ["anonymous", "anonymous"]


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_authenticated_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        response = _login()
    assert response.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] is not None


@pytest.mark.unit
def test_login_failure_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
    rate_limit_store: FakeRateLimitStore,
) -> None:
    candidate = "attacker-candidate-name"
    with caplog.at_level(logging.INFO):
        with shared_rate_limiter(rate_limit_store):
            with mock_db_connection():
                with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure",
                        return_value={"id": "evt"},
                    ):
                        _login(username=candidate, password="wrong-password")
    combined = " ".join(record.getMessage() for record in caplog.records)
    combined += str(getattr(caplog, "text", ""))
    for forbidden in (
        candidate,
        TEST_SOURCE,
        TEST_LIMITER_SECRET,
        f"src:{candidate.lower()}",
        f"acct:{candidate.lower()}",
    ):
        assert forbidden not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != _plain_sha256_identifier("src", TEST_SOURCE.lower())

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert TEST_USERNAME not in str(append_kwargs)
