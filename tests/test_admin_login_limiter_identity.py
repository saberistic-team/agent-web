"""Tests for keyed admin login limiter identifiers and anonymous failure actors (#242)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.actor_context import anonymous_actor_context
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.admin_secrets import validate_admin_security_config
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    _fetch_login_form,
    mock_db_connection,
    shared_rate_limiter,
    FakeRateLimitStore,
)

client = TestClient(app, follow_redirects=False)

CURRENT_SECRET = TEST_LIMITER_SECRET
OTHER_SECRET = "alternate-limiter-key-32-bytes-min!"
PREVIOUS_SECRET = "previous-limiter-key-32-bytes-min!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings_with_secrets(
    *,
    current: str = CURRENT_SECRET,
    previous: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=current,
        admin_login_limiter_secret_previous=previous,
    )


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings_with_secrets()
    source = "203.0.113.50"
    key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = hashlib.sha256(f"src:{source.lower()}".encode("utf-8")).hexdigest()
    assert key != plain
    assert len(key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secrets(current=CURRENT_SECRET)
    settings_b = _settings_with_secrets(current=OTHER_SECRET)
    source = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(source, settings=settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings=settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    settings = _settings_with_secrets()
    first = admin_auth.build_account_rate_limit_key("operator", settings=settings)
    second = admin_auth.build_account_rate_limit_key("operator", settings=settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings_with_secrets()
    payload = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    [
        "",
        "short",
        "placeholder-secret-32-bytes-minimum!",
    ],
)
def test_limiter_secret_validation_rejects_weak_or_missing(secret: str) -> None:
    settings = _settings_with_secrets(current=secret)
    with pytest.raises(ValueError):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_key() -> None:
    settings = _settings_with_secrets(current=CURRENT_SECRET, previous=CURRENT_SECRET)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_limiter_rotation_uses_distinct_current_and_previous_keys() -> None:
    settings = _settings_with_secrets(current=CURRENT_SECRET, previous=PREVIOUS_SECRET)
    source = "203.0.113.88"
    current_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    legacy_keys = admin_auth.login_limiter_legacy_keys(
        submitted_username=TEST_USERNAME,
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(legacy_keys) == 2
    previous_key = admin_auth.build_source_rate_limit_key(
        source,
        settings=settings,
        limiter_secret=PREVIOUS_SECRET,
    )
    assert previous_key in legacy_keys
    assert current_key != previous_key


@pytest.mark.unit
def test_legacy_lock_blocks_admission_without_incrementing_current_key(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_SECRET)
    settings = get_settings()
    source = "203.0.113.77"
    legacy_keys = admin_auth.login_limiter_legacy_keys(
        submitted_username=TEST_USERNAME,
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    now = datetime.now(timezone.utc)
    for key in legacy_keys:
        rate_limit_store.rows[key] = {
            "failure_count": 5,
            "window_started_at": now,
            "locked_until": now.replace(year=now.year + 1),
            "updated_at": now,
        }

    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "corr-legacy-lock"
    request.client = MagicMock()
    request.client.host = "203.0.113.77"
    request.cookies = {}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            result = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username=TEST_USERNAME,
            )

    assert not result.admitted
    assert result.already_locked
    current_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    for key in current_keys:
        assert key not in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, Any]] = []

    def capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": len(captured)}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=capture_failure,
                ):
                    candidate = "attacker-candidate-242"
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": candidate,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    assert captured
                    event = captured[-1]
                    assert event["actor_context"].actor == "anonymous"
                    payload = json.dumps(event, default=str)
                    assert candidate not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, Any]] = []

    def capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": len(captured)}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=capture_failure,
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
                    event = captured[-1]
                    assert event["actor_context"].actor == "anonymous"
                    assert TEST_USERNAME not in json.dumps(event, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_records_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: list[dict[str, Any]] = []

    def capture_failure(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": len(captured)}

    csrf_token, cookies = _fetch_login_form()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=capture_failure,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": "bad-csrf-token",
                    },
                    cookies=cookies,
                )
                assert response.status_code == 400
                assert captured[-1]["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 303
                    actor = success_audit.call_args.kwargs["actor_context"].actor
                    assert actor == TEST_USERNAME


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_secrets(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "attacker-log-candidate-242"
    caplog.set_level(logging.WARNING)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    combined = caplog.text
    assert candidate not in combined
    assert CURRENT_SECRET not in caplog.text
    assert f"src:{candidate}" not in caplog.text


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter identity tests")


@contextmanager
def _pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def pg_conn() -> Generator[psycopg.Connection, None, None]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
        bootstrap.commit()
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
def test_postgres_persists_hmac_limiter_keys_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_SECRET)
    settings = get_settings()
    source = "203.0.113.242"
    limiter_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(limiter_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (limiter_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == limiter_key
    assert row["limiter_key"] != plain
    assert len(row["limiter_key"]) == 64

    repo = PostgresAuditEventRepository()
    candidate = "pg-attacker-candidate-242"
    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "corr-pg-242"
    actor_context = anonymous_actor_context(request)
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor_context,
            reason="invalid_credentials",
            repository=repo,
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    serialized = json.dumps(
        {
            "actor": audit_row["actor"],
            "summary_after": audit_row["summary_after"],
            "metadata": audit_row["metadata"],
        }
    )
    assert candidate not in serialized


@pytest.mark.integration
def test_rotation_cleanup_retains_previous_key_rows_until_expired(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "203.0.113.199"
    previous_key = admin_auth.build_source_rate_limit_key(
        source,
        settings=_settings_with_secrets(current=CURRENT_SECRET),
        limiter_secret=PREVIOUS_SECRET,
    )
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_key,),
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
