"""Keyed limiter identifiers and anonymous failed-login audit actors (#242)."""

from __future__ import annotations

import hashlib
import logging
import os
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
from app.actor_context import ActorContext
from app.admin_auth import LIMITER_KEY_DOMAIN_ACCOUNT, LIMITER_KEY_DOMAIN_SOURCE
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as admin_conn,
        patch("app.admin_auth.db.try_admit_admin_login") as try_admit,
        patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits", return_value=0),
        patch("app.admin_auth.db.db_connection") as auth_conn,
    ):
        try_admit.return_value = db.AdminLoginAdmission(
            admitted=True,
            throttled=False,
            already_locked=False,
            lockout_transition=False,
        )
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        admin_conn.return_value.__enter__.return_value = conn
        admin_conn.return_value.__exit__.return_value = None
        yield conn


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
        from_email="noreply@example.com",
        notify_email="inbox@example.com",
        base_url="http://testserver",
        plausible_domain="",
        plausible_api_key="",
        analytics_environment="development",
        admin_username=TEST_USERNAME,
        admin_password_hash=TEST_HASH,
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_previous,
    )


@pytest.fixture(autouse=True)
def limiter_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    keyed = admin_auth.build_source_rate_limit_key(settings, source)
    plain = admin_auth._plain_sha256_limiter_key(LIMITER_KEY_DOMAIN_SOURCE, source)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_LIMITER_SECRET_ALT)
    source = "203.0.113.1"
    assert admin_auth.build_source_rate_limit_key(settings_a, source) != (
        admin_auth.build_source_rate_limit_key(settings_b, source)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    source = "203.0.113.42"
    first = admin_auth.build_source_rate_limit_key(settings, source)
    second = admin_auth.build_source_rate_limit_key(settings, source)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation() -> None:
    settings = _settings()
    payload = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(settings, payload)
    account_key = admin_auth.build_account_rate_limit_key(settings, payload)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", ""),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "changeme"),
        ("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_or_missing_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    if env_name == "ADMIN_LOGIN_LIMITER_SECRET":
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
        if value:
            monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", value)
    else:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", value)
    settings = get_settings()
    with pytest.raises(ValueError):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_rotation_includes_previous_key_variants() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.9",
        configured_admin_username=TEST_USERNAME,
    )
    current_source = admin_auth.build_source_rate_limit_key(settings, "203.0.113.9")
    previous_source = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_PREVIOUS,
        LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.9",
    )
    current_account = admin_auth.build_account_rate_limit_key(settings, TEST_USERNAME)
    previous_account = admin_auth._digest_limiter_key(
        TEST_LIMITER_SECRET_PREVIOUS,
        LIMITER_KEY_DOMAIN_ACCOUNT,
        TEST_USERNAME,
    )
    assert current_source in keys
    assert previous_source in keys
    assert current_account in keys
    assert previous_account in keys


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter identity tests")


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
def test_rotation_previous_key_rows_remain_enforceable_and_cleanup_eligible(
    pg_conn: psycopg.Connection,
) -> None:
    previous_settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        limiter_previous="",
    )
    previous_key = admin_auth.build_source_rate_limit_key(previous_settings, "203.0.113.88")
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(5):
        admission = db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(previous_key,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted
    pg_conn.commit()

    rotated_settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    rotated_keys = admin_auth.login_limiter_keys(
        settings=rotated_settings,
        submitted_username="ghost",
        client_source="203.0.113.88",
        configured_admin_username=TEST_USERNAME,
    )
    blocked = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=rotated_keys,
        now=now + timedelta(seconds=10),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=2000),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@pytest.mark.integration
def test_hmac_limiter_rows_persist_fixed_length_identifiers(pg_conn: psycopg.Connection) -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(settings, "203.0.113.77")
    now = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
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
    assert len(row["limiter_key"]) == 64
    assert row["limiter_key"] == source_key
    plain = hashlib.sha256(b"src:203.0.113.77").hexdigest()
    assert row["limiter_key"] != plain


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-1"}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "attacker-candidate",
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert "attacker-candidate" not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-2"}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
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
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert TEST_USERNAME not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-3"}

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": TEST_PASSWORD,
                        "csrf_token": "bad-csrf",
                    },
                )
    assert response.status_code == 400
    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_authenticated_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
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
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME


@pytest.mark.unit
def test_failed_login_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "candidate-user@example.com"
    caplog.set_level(logging.INFO)
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure"):
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
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


@pytest.mark.integration
def test_postgres_login_failure_audit_row_actor_is_anonymous(pg_conn: psycopg.Connection) -> None:
    from app.repositories.postgres import PostgresAuditEventRepository

    repo = PostgresAuditEventRepository()
    actor = ActorContext(actor="anonymous", correlation_id="corr-pg-242")
    audit_service.record_login_failure(
        pg_conn,
        actor_context=actor,
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
        row = cur.fetchone()
    assert row is not None
    assert row["actor"] == "anonymous"
    assert "operator" not in str(row["summary_after"])
    assert "operator" not in str(row["metadata"])
