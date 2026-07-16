"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    AdminSecurityConfigError,
    digest_limiter_key,
    plain_sha256_limiter_key,
    validate_admin_security_config,
)
from app.config import Settings, get_settings
from app.actor_context import ActorContext
from app.main import app
from app.migrations.runner import apply_migrations

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"
CANDIDATE_USERNAME = "attacker-candidate-user"
TEST_SOURCE = "203.0.113.50"

client = TestClient(app, follow_redirects=False)


def _settings(**overrides: str) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SESSION_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
    }
    base.update(overrides)
    return Settings(
        database_url=base["DATABASE_URL"],
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url=base["BASE_URL"],
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=base["ADMIN_USERNAME"],
        admin_password_hash=base["ADMIN_PASSWORD_HASH"],
        admin_session_secret=base["ADMIN_SESSION_SECRET"],
        admin_login_limiter_secret=base.get("ADMIN_LOGIN_LIMITER_SECRET", ""),
        admin_login_limiter_previous_secret=base.get(
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", ""
        ),
    )


@pytest.fixture(autouse=True)
def limiter_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)

    assert source_key != plain_sha256_limiter_key(LIMITER_DOMAIN_SOURCE, TEST_SOURCE)
    assert account_key != plain_sha256_limiter_key(
        LIMITER_DOMAIN_ACCOUNT, TEST_USERNAME
    )
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_identifier_depends_on_secret() -> None:
    settings_a = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET)
    settings_b = _settings(
        ADMIN_LOGIN_LIMITER_SECRET="other-limiter-secret-32chars-minimum!!"
    )
    key_a = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_identifier_stable_for_identical_inputs() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    assert first == second
    assert first == digest_limiter_key(TEST_LIMITER_SECRET, LIMITER_DOMAIN_SOURCE, TEST_SOURCE)


@pytest.mark.unit
def test_domain_separation_for_identical_payload() -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("shared-value", settings)
    account_key = admin_auth.build_account_rate_limit_key("shared-value", settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_name,value",
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", "short"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "changeme-changeme-changeme-changeme"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "placeholder-secret-32chars-minimum!!"),
        ("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", "weak"),
    ],
)
def test_secret_validation_rejects_invalid_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv(env_name, value)
    settings = get_settings()
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_secret_validation_requires_limiter_secret_when_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    assert not settings.admin_auth_configured
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_secret_validation_rejects_matching_rotation_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_honors_previous_secret_lockout() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET=TEST_PREVIOUS_LIMITER_SECRET,
    )
    previous_key = digest_limiter_key(
        TEST_PREVIOUS_LIMITER_SECRET, LIMITER_DOMAIN_SOURCE, TEST_SOURCE
    )
    current_key = admin_auth.build_source_rate_limit_key(TEST_SOURCE, settings)
    assert previous_key != current_key

    rotation_keys = admin_auth.login_limiter_rotation_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    assert previous_key in rotation_keys
    assert current_key not in rotation_keys


@pytest.mark.integration
def test_rotation_previous_rows_remain_eligible_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (pytest.importorskip("os").environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    previous_key = digest_limiter_key(
        TEST_PREVIOUS_LIMITER_SECRET, LIMITER_DOMAIN_SOURCE, TEST_SOURCE
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (previous_key, 1, now, None, now),
        )
        conn.commit()
        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now + timedelta(seconds=2000),
            window_seconds=60,
            lockout_seconds=60,
        )
        conn.commit()
    assert deleted >= 1


class _AuditSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> dict[str, Any]:
        payload = {
            "actor": actor_context.actor,
            "reason": reason,
            "correlation_id": actor_context.correlation_id,
        }
        self.calls.append(payload)
        return {"id": len(self.calls)}


@contextmanager
def _mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = "wrong-password",
    csrf_token: str = "flow-csrf",
) -> Any:
    return client.post(
        "/admin/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_token,
        },
    )


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor() -> None:
    spy = _AuditSpy()
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=spy.record,
            ):
                response = _login(username=CANDIDATE_USERNAME)
    assert response.status_code == 401
    assert len(spy.calls) == 1
    event = spy.calls[0]
    assert event["actor"] == "anonymous"
    assert CANDIDATE_USERNAME not in json.dumps(event)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    spy = _AuditSpy()
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=spy.record,
            ):
                response = _login(username=TEST_USERNAME, password="wrong-password")
    assert response.status_code == 401
    assert spy.calls[0]["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor() -> None:
    spy = _AuditSpy()
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy.record,
                ):
                    response = _login(csrf_token="bad-csrf")
    assert response.status_code == 400
    assert spy.calls[0]["actor"] == "anonymous"
    assert spy.calls[0]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor() -> None:
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    response = _login(password=TEST_PASSWORD)
    assert response.status_code == 303
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_exclude_candidate_and_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure", return_value=None):
                _login(username=CANDIDATE_USERNAME, password="wrong-password")
    combined = caplog.text
    assert CANDIDATE_USERNAME not in combined
    assert TEST_SOURCE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


@pytest.mark.integration
def test_postgres_rows_store_keyed_identifiers_and_anonymous_actors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    database_url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "1")
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    plain = plain_sha256_limiter_key(LIMITER_DOMAIN_SOURCE, "testclient")
    assert source_key != plain

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        actor_context = ActorContext(actor="anonymous", correlation_id="corr-db")
        captured_actor = ""

        def _capture_append(**kwargs: Any) -> dict[str, Any]:
            nonlocal captured_actor
            captured_actor = str(kwargs.get("actor"))
            return {"id": "evt-1"}

        repo = MagicMock()
        repo.append.side_effect = _capture_append
        audit_service.record_login_failure(
            conn,
            actor_context=actor_context,
            reason="invalid_credentials",
            repository=repo,
        )
        assert captured_actor == "anonymous"

        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(source_key,),
            now=now,
            rate_limit=1,
            window_seconds=900,
            lockout_seconds=900,
        )
        conn.commit()
        assert admission.admitted

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (source_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["limiter_key"] == source_key
        assert row["limiter_key"] != plain
        assert len(row["limiter_key"]) == 64
