"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import mock_db_connection

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
ALT_LIMITER_SECRET = "alt-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "prev-limiter-secret-32chars-minimum!"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: Any) -> Settings:
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
        "admin_login_limiter_previous_secret": base.admin_login_limiter_previous_secret,
    }
    fields.update(overrides)
    return Settings(**fields)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    keyed = admin_auth.build_source_rate_limit_key(source, settings=settings)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    source = "203.0.113.1"
    assert admin_auth.build_source_rate_limit_key(source, settings=settings_a) != (
        admin_auth.build_source_rate_limit_key(source, settings=settings_b)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_secret() -> None:
    settings = _settings()
    source = "203.0.113.1"
    first = admin_auth.build_source_rate_limit_key(source, settings=settings)
    second = admin_auth.build_source_rate_limit_key(source, settings=settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(material, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(material, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-placeholder-secret-value-here!!", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    env_name: str,
) -> None:
    with pytest.raises(ValueError, match=env_name):
        admin_auth.validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_LIMITER_SECRET)
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_security_config(get_settings())


@pytest.mark.unit
def test_startup_validation_fails_for_missing_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        admin_auth.validate_admin_security_config(get_settings())


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    cookies = {}
    for header in response.headers.get_list("set-cookie"):
        name, value = header.split("=", 1)
        cookies[name] = value.split(";", 1)[0]
    return match.group(1), cookies


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str | None = None,
    cookies: dict[str, str] | None = None,
) -> Any:
    if csrf_token is None or cookies is None:
        csrf_token, cookies = _fetch_login_form()
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        cookies=cookies,
    )


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_records_anonymous_actor_only() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                candidate = "attacker-supplied@example.com"
                response = _login(username=candidate, password="wrong-password")
    assert response.status_code == 401
    failure_audit.assert_called_once()
    actor_context = failure_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == "anonymous"
    payload = json.dumps(failure_audit.call_args.kwargs, default=str)
    assert candidate not in payload
    assert "attempted_username" not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                response = _login(password="wrong-password")
    assert response.status_code == 401
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(failure_audit.call_args.kwargs, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_is_anonymous_without_candidate() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost-user",
                        "password": "wrong-password",
                        "csrf_token": csrf_token + "x",
                    },
                    cookies=cookies,
                )
    assert response.status_code == 400
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert "ghost-user" not in json.dumps(failure_audit.call_args.kwargs, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_administrator_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    response = _login()
    assert response.status_code == 303
    actor_context = success_audit.call_args.kwargs["actor_context"]
    assert actor_context.actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_secret_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    candidate = "logged-candidate@example.com"
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = _login(username=candidate, password="wrong-password")
    assert response.status_code == 401
    combined = caplog.text + response.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113" not in combined


@pytest.mark.unit
def test_rotation_honors_previous_key_lockout_without_incrementing_it() -> None:
    settings = _settings(
        admin_login_limiter_secret=ALT_LIMITER_SECRET,
        admin_login_limiter_previous_secret=PREVIOUS_LIMITER_SECRET,
    )
    source = "203.0.113.88"
    previous_key = admin_auth._digest_limiter_key(
        admin_auth._LIMITER_DOMAIN_SOURCE,
        source,
        PREVIOUS_LIMITER_SECRET.encode("utf-8"),
    )
    current_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        {
            "limiter_key": previous_key,
            "failure_count": 5,
            "window_started_at": now,
            "locked_until": now + timedelta(minutes=15),
        },
        {
            "limiter_key": current_key,
            "failure_count": 0,
            "window_started_at": now,
            "locked_until": None,
        },
    ]

    guard_keys = admin_auth.login_limiter_guard_keys(
        submitted_username="ghost",
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    write_keys = admin_auth.login_limiter_write_keys(
        submitted_username="ghost",
        client_source=source,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )

    admission = db.try_admit_admin_login(
        conn,
        limiter_keys=guard_keys,
        increment_keys=write_keys,
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    assert not admission.admitted
    assert admission.already_locked
    update_calls = [
        call
        for call in cursor.execute.call_args_list
        if call.args and "UPDATE admin_login_rate_limits" in call.args[0]
    ]
    assert not update_calls


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_rows_and_anonymous_failure_actor() -> None:
    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    required = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not url:
        if required:
            pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
        pytest.skip("TEST_DATABASE_URL not set")

    settings = _settings()
    source = "203.0.113.242"
    source_key = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    assert source_key != plain

    with psycopg.connect(url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        apply_migrations(conn)

        now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            increment_keys=(source_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )

        row = conn.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        ).fetchone()
        assert row is not None
        assert row["limiter_key"] == source_key
        assert row["limiter_key"] != plain

        audit_service.record_login_failure(
            conn,
            actor_context=ActorContext(actor="anonymous", correlation_id="corr-242"),
            reason="invalid_credentials",
        )
        conn.commit()

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
        assert audit_row is not None
        assert audit_row["actor"] == "anonymous"
        summary = audit_row["summary_after"] or {}
        metadata = audit_row["metadata"] or {}
        assert TEST_USERNAME not in json.dumps(summary)
        assert TEST_USERNAME not in json.dumps(metadata)
        assert "attempted_username" not in json.dumps(metadata)

        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.commit()
