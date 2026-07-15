"""Tests for keyed admin login limiter identifiers and secret validation."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import patch

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations

TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        "$argon2id$v=19$m=65536,t=3,p=4$aaaaaaaaaaaaaaaaaaaaaa$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    if not overrides:
        return base
    return Settings(
        database_url=overrides.get("database_url", base.database_url),
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
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_login_limiter_secret=overrides.get(
            "admin_login_limiter_secret", base.admin_login_limiter_secret
        ),
        admin_login_limiter_secret_previous=overrides.get(
            "admin_login_limiter_secret_previous", base.admin_login_limiter_secret_previous
        ),
        admin_session_ttl_seconds=base.admin_session_ttl_seconds,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
        admin_trust_proxy_headers=base.admin_trust_proxy_headers,
        audit_page_size=base.audit_page_size,
        brief_page_size=base.brief_page_size,
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    keyed = admin_auth.build_source_rate_limit_key(source, settings)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET_ALT)
    source = "203.0.113.1"
    assert admin_auth.build_source_rate_limit_key(source, settings_a) != (
        admin_auth.build_source_rate_limit_key(source, settings_b)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_secret() -> None:
    settings = _settings()
    source = "203.0.113.1"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_source_and_account() -> None:
    settings = _settings()
    payload = "operator"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret,env_name",
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("this-is-a-placeholder-secret-value-32", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-limiter-secret-32chars-minimum", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, env_name: str) -> None:
    with pytest.raises(ValueError):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_security_secrets(get_settings())


@pytest.mark.unit
def test_rotation_throttle_keys_include_previous_secret() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    current = admin_auth.login_limiter_keys(
        submitted_username="operator",
        client_source="203.0.113.1",
        configured_admin_username="operator",
        settings=settings,
    )
    throttle = admin_auth.login_limiter_throttle_keys(
        submitted_username="operator",
        client_source="203.0.113.1",
        configured_admin_username="operator",
        settings=settings,
    )
    assert len(throttle) > len(current)
    previous_only = tuple(key for key in throttle if key not in current)
    assert previous_only


@pytest.mark.unit
def test_rotation_previous_lockout_blocks_admission_without_incrementing_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_admin_auth import FakeRateLimitStore, shared_rate_limiter

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    store = FakeRateLimitStore()
    now = datetime.now(timezone.utc)
    throttle_keys = admin_auth.login_limiter_throttle_keys(
        submitted_username="operator",
        client_source="testclient",
        configured_admin_username=settings.admin_username,
        settings=settings,
    )
    write_keys = admin_auth.login_limiter_keys(
        submitted_username="operator",
        client_source="testclient",
        configured_admin_username=settings.admin_username,
        settings=settings,
    )
    previous_keys = [key for key in throttle_keys if key not in write_keys]
    assert previous_keys
    store.rows[previous_keys[0]] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(minutes=15),
        "updated_at": now,
    }

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
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)

    with shared_rate_limiter(store):
        result = admin_auth.try_admit_login_attempt(request, settings, username="operator")

    assert not result.admitted
    assert result.already_locked
    assert all(key not in store.rows for key in write_keys)


_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter key tests")


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        apply_migrations(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source = "203.0.113.88"
    limiter_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)

    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(limiter_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted

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


@pytest.mark.unit
def test_limiter_logs_do_not_include_secret_or_raw_source(caplog: pytest.LogCaptureFixture) -> None:
    from starlette.requests import Request

    settings = get_settings()
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
        "client": ("203.0.113.77", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)

    with caplog.at_level(logging.INFO, logger="app.admin_auth"):
        with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("down")):
            with patch.object(admin_auth, "_is_fallback_throttled", return_value=False):
                admin_auth.try_admit_login_attempt(request, settings, username="ghost-user")

    combined = " ".join(record.getMessage() for record in caplog.records)
    for secret in (
        TEST_LIMITER_SECRET,
        "src:203.0.113.77",
        "acct:ghost-user",
        "203.0.113.77",
        "ghost-user",
    ):
        assert secret not in combined


@pytest.mark.integration
def test_postgres_login_failure_audit_persists_anonymous_actor(
    pg_conn: psycopg.Connection,
) -> None:
    from app.actor_context import ActorContext
    from app.audit_service import ACTION_AUTH_LOGIN_FAILURE
    from app.repositories.postgres import PostgresAuditEventRepository

    candidate = "attacker-supplied-actor-name"
    repo = PostgresAuditEventRepository()
    audit_service.record_login_failure(
        pg_conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg-1"),
        reason="invalid_credentials",
        repository=repo,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ACTION_AUTH_LOGIN_FAILURE,),
        )
        row = cur.fetchone()

    assert row is not None
    assert row["actor"] == "anonymous"
    serialized = str(row["summary_after"]) + str(row["metadata"])
    assert candidate not in serialized

