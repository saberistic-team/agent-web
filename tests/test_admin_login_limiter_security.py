"""Security tests for keyed login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

pytest_plugins = ["tests.test_admin_auth"]

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    _fetch_login_form,
    _login,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum"

_REQUIRED = (__import__("os").environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {
    "1",
    "true",
    "yes",
}
_DATABASE_URL = (__import__("os").environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_identifier(prefix: str, material: str) -> str:
    return hashlib.sha256(f"{prefix}:{material}".encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings)
    assert source_key != _plain_sha256_identifier("src", "203.0.113.1")
    assert account_key != _plain_sha256_identifier("acct", "operator")
    assert len(source_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = get_settings()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_ALT)
    settings_b = get_settings()
    key_a = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_a)
    key_b = admin_auth.build_source_rate_limit_key("203.0.113.1", settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_across_processes() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = get_settings()
    shared_material = "operator"
    source_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        shared_material,
        settings.admin_login_limiter_secret,
    )
    account_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_ACCOUNT,
        shared_material,
        settings.admin_login_limiter_secret,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "secret_value", "match"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", "", "required"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short", "at least"),
        (
            "ADMIN_LOGIN_LIMITER_SECRET",
            "changeme-changeme-changeme-changeme!!",
            "placeholder",
        ),
        (
            "ADMIN_LOGIN_LIMITER_SECRET",
            f"  {TEST_LIMITER_SECRET}  ",
            "whitespace",
        ),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    secret_value: str,
    match: str,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(env_name, secret_value)
    settings = get_settings()
    with pytest.raises(ValueError, match=match):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_identical_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_previous_key_still_blocks_admission(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()
    previous_key = admin_auth._login_limiter_keys_with_secret(
        submitted_username=TEST_USERNAME,
        client_source="testclient",
        configured_admin_username=TEST_USERNAME,
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )[0]
    now = datetime.now(timezone.utc)
    rate_limit_store.rows[previous_key] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now + timedelta(seconds=900),
        "updated_at": now,
    }

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            response = _login(password="wrong")
            assert response.status_code == 429


@pytest.mark.unit
def test_rotation_cleanup_removes_stale_previous_key_rows(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    stale_key = "deadbeef" * 8
    rate_limit_store.rows[stale_key] = {
        "failure_count": 1,
        "window_started_at": now - timedelta(hours=2),
        "locked_until": None,
        "updated_at": now - timedelta(hours=2),
    }
    deleted = rate_limit_store.cleanup(
        now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    assert stale_key not in rate_limit_store.rows


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy):
                response = _login(username="ghost-attacker", password="wrong")
                assert response.status_code == 401

    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert "ghost-attacker" not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy):
                response = _login(password="wrong")
                assert response.status_code == 401

    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "candidate-user",
                        "password": TEST_PASSWORD,
                        "csrf_token": "bad-token",
                    },
                    cookies=cookies,
                )
                assert response.status_code == 400

    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_csrf"
    assert "candidate-user" not in json.dumps(captured, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured: list[dict[str, Any]] = []

    def _spy(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy):
                assert _login(password="wrong").status_code == 401
                assert _login(password="wrong").status_code == 401

    assert captured[-1]["actor_context"].actor == "anonymous"
    assert captured[-1]["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes.audit_service.record_login_success", side_effect=_spy):
                response = _login()
                assert response.status_code == 303

    assert captured["actor_context"].actor == TEST_USERNAME
    assert captured["session_id"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_do_not_leak_candidates(
    rate_limit_store: FakeRateLimitStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "secret-candidate-user"
    source = "203.0.113.77"
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with caplog.at_level(logging.WARNING):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                    headers={"X-Forwarded-For": source},
                )
                assert response.status_code == 401

    combined = caplog.text + response.text
    assert candidate not in combined
    assert source not in combined
    assert TEST_LIMITER_SECRET not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.fixture
def pg_conn(monkeypatch: pytest.MonkeyPatch) -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    monkeypatch.setenv("DATABASE_URL", database_url)
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
        bootstrap.commit()
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != _plain_sha256_identifier("src", "203.0.113.88")

    repo = PostgresAuditEventRepository()
    audit_service.record_login_failure(
        pg_conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-test"),
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
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        )
        audit_row = cur.fetchone()
    assert audit_row is not None
    assert audit_row["actor"] == "anonymous"
    payload = json.dumps(audit_row, default=str)
    assert TEST_USERNAME not in payload
    assert "ghost" not in payload
