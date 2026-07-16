"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

pytest_plugins = ["tests.test_admin_auth"]

import hashlib
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_secrets import AdminSecretValidationError, validate_admin_login_limiter_secret
from app.admin_security import validate_admin_security_config
from app.actor_context import anonymous_actor_context
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    FakeRateLimitStore,
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

CURRENT_LIMITER_SECRET = "current-limiter-secret-32chars-minimum!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-minimum!"


def _settings(
    *,
    limiter_secret: str = CURRENT_LIMITER_SECRET,
    limiter_previous: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost:5432/test",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_previous,
    )


def _plain_sha256_limiter_digest(domain: str, material: str) -> str:
    payload = f"{domain}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.50"
    account = TEST_USERNAME
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)
    assert source_key != _plain_sha256_limiter_digest("src", source)
    assert account_key != _plain_sha256_limiter_digest("acct", account)
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=CURRENT_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=PREVIOUS_LIMITER_SECRET)
    source = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    settings = _settings()
    source = "203.0.113.50"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.digest_limiter_key(
        CURRENT_LIMITER_SECRET,
        admin_auth._LIMITER_DOMAIN_SOURCE,
        source,
    )
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current", "previous", "match"),
    [
        ("", None, "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short-secret", None, "must be at least 32 characters"),
        ("changeme-changeme-changeme-changeme!", None, "placeholder"),
        (
            CURRENT_LIMITER_SECRET,
            CURRENT_LIMITER_SECRET,
            "must differ",
        ),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    current: str,
    previous: str | None,
    match: str,
) -> None:
    with pytest.raises(AdminSecretValidationError, match=match):
        validate_admin_login_limiter_secret(current, previous=previous)


@pytest.mark.unit
def test_validate_admin_security_config_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(AdminSecretValidationError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys() -> None:
    settings = _settings(
        limiter_secret=CURRENT_LIMITER_SECRET,
        limiter_previous=PREVIOUS_LIMITER_SECRET,
    )
    source_keys = admin_auth.build_source_rate_limit_keys("203.0.113.50", settings)
    assert len(source_keys) == 2
    assert admin_auth.build_source_rate_limit_key("203.0.113.50", settings) in source_keys
    previous_only = admin_auth.digest_limiter_key(
        PREVIOUS_LIMITER_SECRET,
        admin_auth._LIMITER_DOMAIN_SOURCE,
        "203.0.113.50",
    )
    assert previous_only in source_keys


@pytest.mark.unit
def test_login_limiter_keys_include_rotation_account_buckets() -> None:
    settings = _settings(
        limiter_secret=CURRENT_LIMITER_SECRET,
        limiter_previous=PREVIOUS_LIMITER_SECRET,
    )
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.50",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(keys) == 4


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor_only(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    captured: list[dict[str, Any]] = []

    def _spy(
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> None:
        captured.append(
            {
                "actor": actor_context.actor,
                "reason": reason,
                "repository": repository,
            }
        )

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_spy,
        ), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": "attacker-supplied-name",
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert response.status_code == 401
    assert len(captured) == 1
    assert captured[0]["actor"] == "anonymous"
    assert captured[0]["reason"] == "invalid_credentials"
    assert "attacker-supplied-name" not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_actor_remains_anonymous(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    captured: list[str] = []

    def _spy(conn: Any, *, actor_context: Any, reason: str, repository: Any = None) -> None:
        captured.append(actor_context.actor)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_spy,
        ), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert response.status_code == 401
    assert captured == ["anonymous"]


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_flow_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    captured: list[tuple[str, str]] = []

    def _spy(conn: Any, *, actor_context: Any, reason: str, repository: Any = None) -> None:
        captured.append((actor_context.actor, reason))

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_spy,
        ):
            invalid_flow = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": TEST_PASSWORD,
                    "csrf_token": "flow-csrf",
                },
            )
    assert invalid_flow.status_code == 400
    assert captured == [("anonymous", "invalid_csrf")]


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_uses_anonymous_actor(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured: list[tuple[str, str]] = []

    def _spy(conn: Any, *, actor_context: Any, reason: str, repository: Any = None) -> None:
        captured.append((actor_context.actor, reason))

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_spy,
        ), patch("app.admin_routes._try_claim_login_flow", return_value=True):
            assert (
                client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                ).status_code
                == 401
            )
            lockout = client.post(
                "/admin/login",
                data={
                    "username": TEST_USERNAME,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert lockout.status_code == 401
    assert captured[-1] == ("anonymous", "rate_limited")


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes._try_claim_login_flow", return_value=True
        ), patch("app.admin_routes.db.create_admin_session", return_value=42), patch(
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
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_exclude_candidate_and_secret_material(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    candidate = "attacker-supplied-name"
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection(), patch(
            "app.admin_routes._try_claim_login_flow", return_value=True
        ):
            response = client.post(
                "/admin/login",
                data={
                    "username": candidate,
                    "password": "wrong-password",
                    "csrf_token": "flow-csrf",
                },
            )
    assert response.status_code == 401
    combined = caplog.text
    assert candidate not in combined
    assert CURRENT_LIMITER_SECRET not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113." not in combined


@pytest.mark.unit
def test_audit_login_failure_helper_never_persists_attempted_username() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-auth"}
    request = MagicMock()
    request.headers.get.return_value = "corr-auth"
    actor = anonymous_actor_context(request)
    audit_service.record_login_failure(
        conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert append_kwargs["metadata"] == {"reason": "invalid_credentials"}
    assert "ghost" not in str(append_kwargs)


def _require_database_url() -> str:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")
    return database_url


@contextmanager
def _connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)
        bootstrap.commit()
    with _connect(database_url) as conn:
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
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    settings = _settings()
    source = "203.0.113.88"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    assert source_key != _plain_sha256_limiter_digest("src", source)

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
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

    actor = anonymous_actor_context(
        MagicMock(headers=MagicMock(get=MagicMock(return_value="pg-corr")))
    )
    with crm_transaction(pg_conn):
        audit_service.record_login_failure(
            pg_conn,
            actor_context=actor,
            reason="invalid_credentials",
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
    assert TEST_USERNAME not in str(audit_row["summary_after"])
    assert TEST_USERNAME not in str(audit_row["metadata"])
    assert source not in str(audit_row)


@pytest.mark.integration
def test_rotation_previous_key_rows_remain_eligible_for_cleanup(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", CURRENT_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = _settings(
        limiter_secret=CURRENT_LIMITER_SECRET,
        limiter_previous=PREVIOUS_LIMITER_SECRET,
    )
    source = "203.0.113.89"
    keys = admin_auth.build_source_rate_limit_keys(source, settings)
    assert len(keys) == 2
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=keys,
        now=now,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    pg_conn.commit()

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
