"""Tests for HMAC admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations
from app.repositories.postgres import PostgresAuditEventRepository

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum"


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
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
        admin_session_secret="test-session-secret-32chars-minimum",
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_previous,
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    keyed = admin_auth.build_source_rate_limit_key(source, settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    left = admin_auth.build_source_rate_limit_key(
        "203.0.113.10",
        _settings(limiter_secret=TEST_LIMITER_SECRET),
    )
    right = admin_auth.build_source_rate_limit_key(
        "203.0.113.10",
        _settings(limiter_secret=TEST_LIMITER_SECRET_ALT),
    )
    assert left != right


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domains_are_separated() -> None:
    settings = _settings()
    payload = "operator"
    source_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        payload,
        settings.admin_login_limiter_secret,
    )
    account_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_ACCOUNT,
        payload,
        settings.admin_login_limiter_secret,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "required"),
        ("short-secret", "at least 32"),
        ("changeme-change-me-change-me-change-me!", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, message: str) -> None:
    settings = replace(_settings(), admin_login_limiter_secret=secret)
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_limiter_previous_secret_must_differ_from_current() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_exposes_previous_keys_for_throttle_checks_only() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    current = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    throttle = admin_auth.login_limiter_throttle_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert len(current) == 2
    assert len(throttle) == 4
    assert set(current).issubset(set(throttle))


@pytest.mark.unit
def test_rotation_previous_key_rows_are_cleaned_up() -> None:
    previous_source = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.66",
        TEST_LIMITER_SECRET_PREVIOUS,
    )
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.rowcount = 1
    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted == 1
    sql = cursor.execute.call_args.args[0]
    assert "admin_login_rate_limits" in sql
    _ = previous_source


@pytest.mark.unit
def test_record_login_failure_persists_anonymous_actor_only() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    actor = ActorContext(actor="anonymous", correlation_id="trace-1")
    audit_service.record_login_failure(
        MagicMock(),
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert append_kwargs["summary_after"] == {"reason": "invalid_credentials"}
    assert TEST_USERNAME not in json.dumps(append_kwargs)


@pytest.mark.unit
def test_successful_login_retains_administrator_actor() -> None:
    repo = MagicMock()
    actor = ActorContext(actor=TEST_USERNAME, correlation_id="trace-2")
    audit_service.record_login_success(
        MagicMock(),
        actor_context=actor,
        session_id=7,
        repository=repo,
    )
    assert repo.append.call_args.kwargs["actor"] == TEST_USERNAME
    assert repo.append.call_args.kwargs["entity_id"] == "7"


@pytest.mark.unit
def test_try_admit_login_attempt_logs_exclude_sensitive_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    scope = {
        "type": "http",
        "headers": [],
        "client": ("203.0.113.10", 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    from starlette.requests import Request

    request = Request(scope)
    caplog.set_level(logging.INFO)
    with patch("app.admin_auth.db.db_connection") as db_conn:
        conn = MagicMock()
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        with patch(
            "app.admin_auth._is_any_limiter_throttled",
            return_value=False,
        ):
            with patch(
                "app.admin_auth.db.try_admit_admin_login",
                return_value=db.AdminLoginAdmission(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=False,
                ),
            ):
                with patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits"):
                    admin_auth.try_admit_login_attempt(
                        request,
                        settings,
                        username="attacker-candidate",
                    )
    blob = caplog.text
    for forbidden in (
        "attacker-candidate",
        TEST_LIMITER_SECRET,
        "203.0.113.10",
        "src:203.0.113.10",
    ):
        assert forbidden not in blob


@pytest.mark.unit
def test_startup_validates_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "short")
    with pytest.raises(ValueError, match="at least 32"):
        admin_auth.validate_admin_security_secrets(get_settings())


def _require_database_url() -> str:
    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
    if database_url:
        return database_url
    if required:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set")


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_failure_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source = "203.0.113.200"
    limiter_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert limiter_key != plain

    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        apply_migrations(conn)

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with psycopg.connect(database_url, autocommit=False) as conn:
        db.try_admit_admin_login(
            conn,
            limiter_keys=(limiter_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        row = conn.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (limiter_key,),
        ).fetchone()
        assert row is not None
        assert row[0] == limiter_key
        assert row[0] != plain

        repo = PostgresAuditEventRepository()
        actor = ActorContext(actor="anonymous", correlation_id="pg-test")
        audit_service.record_login_failure(
            conn,
            actor_context=actor,
            reason="invalid_credentials",
            repository=repo,
        )
        audit_row = conn.execute(
            """
            SELECT actor, summary_after, metadata
            FROM audit_events
            WHERE action = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
        ).fetchone()
        conn.rollback()

    assert audit_row is not None
    assert audit_row[0] == "anonymous"
    payload = json.dumps({"summary_after": audit_row[1], "metadata": audit_row[2]})
    assert TEST_USERNAME not in payload
    assert source not in payload
