"""Tests for HMAC admin login limiter keys and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, db
from app.admin_secrets import validate_admin_secret_value, validate_admin_security_secrets
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET, TEST_LIMITER_SECRET_PREVIOUS

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"

client = TestClient(app, follow_redirects=False)

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(**overrides: str) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "stripe_secret_key": "",
        "stripe_webhook_secret": "",
        "stripe_publishable_key": "",
        "resend_api_key": "",
        "from_email": "noreply@test",
        "notify_email": "inbox@test",
        "base_url": "http://testserver",
        "plausible_domain": "",
        "plausible_api_key": "",
        "analytics_environment": "development",
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SESSION_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_secret_previous": "",
    }
    base.update(overrides)
    return Settings(**base)


def _plain_sha256(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


def _reset_public_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()


@pytest.fixture
def pg_conn() -> Any:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        _reset_public_schema(bootstrap)
        apply_migrations(bootstrap)
    conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            _reset_public_schema(cleanup)


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.1"
    account = "operator"
    keyed_source = admin_auth.build_source_rate_limit_key(source, settings)
    keyed_account = admin_auth.build_account_rate_limit_key(account, settings)
    assert keyed_source != _plain_sha256("src", source)
    assert keyed_account != _plain_sha256("acct", account.lower())
    assert len(keyed_source) == 64
    assert len(keyed_account) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    material = "203.0.113.50"
    first = admin_auth.build_source_rate_limit_key(
        material,
        _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET),
    )
    second = admin_auth.build_source_rate_limit_key(
        material,
        _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS),
    )
    assert first != second


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    material = "203.0.113.50"
    first = admin_auth.build_source_rate_limit_key(material, settings)
    second = admin_auth.build_source_rate_limit_key(material, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    payload = "203.0.113.50"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short", "must be at least 32 characters"),
        ("changeme-changeme-changeme-changeme", "disallowed placeholder"),
    ],
)
def test_limiter_secret_value_validation_fails(secret: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_admin_secret_value(
            secret,
            env_name="ADMIN_LOGIN_LIMITER_SECRET",
        )


@pytest.mark.unit
def test_limiter_previous_secret_value_validation_fails() -> None:
    with pytest.raises(ValueError, match="must be at least 32 characters"):
        validate_admin_secret_value(
            "short",
            env_name="ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS",
        )


@pytest.mark.unit
def test_limiter_rotation_secrets_must_differ() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_secret_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_includes_previous_key_variants() -> None:
    settings = _settings(admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    previous_only = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )
    previous_source = admin_auth.build_source_rate_limit_key(
        "203.0.113.10",
        previous_only,
    )
    assert current_source in keys
    assert previous_source in keys
    assert len(keys) == 4


@pytest.mark.unit
def test_rotation_previous_rows_remain_eligible_for_cleanup() -> None:
    settings = _settings(admin_login_limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    previous_only = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET_PREVIOUS,
        admin_login_limiter_secret_previous="",
    )
    admin_auth.build_source_rate_limit_key("203.0.113.88", previous_only)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    conn = MagicMock()
    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        with patch("app.admin_auth.db.try_admit_admin_login") as admit:
            with patch("app.admin_auth.db.cleanup_expired_admin_login_rate_limits") as cleanup:
                admit.return_value = db.AdminLoginAdmission(
                    admitted=True,
                    throttled=False,
                    already_locked=False,
                    lockout_transition=False,
                )
                admin_auth.try_admit_login_attempt(
                    MagicMock(),
                    settings,
                    username=TEST_USERNAME,
                )
    cleanup.assert_called_once()


@pytest.mark.unit
def test_record_login_failure_repository_spy_uses_anonymous_actor() -> None:
    from app.actor_context import ActorContext
    from app import audit_service

    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-auth"}
    actor = ActorContext(actor="anonymous", correlation_id="corr-auth")

    audit_service.record_login_failure(
        conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )

    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert append_kwargs["summary_after"] == {"reason": "invalid_credentials"}
    assert "ghost" not in str(append_kwargs)


@pytest.mark.unit
def test_configured_username_wrong_password_keeps_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    captured: dict[str, Any] = {}

    def _capture(conn: MagicMock, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.create_admin_login_flow", return_value=1):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                conn = MagicMock()
                db_conn.return_value.__enter__.return_value = conn
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    with patch("app.admin_auth.try_admit_login_attempt") as admit:
                        admit.return_value = admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=False,
                        )
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
    assert TEST_USERNAME not in str(captured)


@pytest.mark.unit
def test_invalid_csrf_audit_uses_anonymous_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    captured: dict[str, Any] = {}

    def _capture(conn: MagicMock, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("app.admin_routes._try_claim_login_flow", return_value=False):
        with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=True):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                conn = MagicMock()
                db_conn.return_value.__enter__.return_value = conn
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    with patch("app.admin_auth.try_admit_login_attempt") as admit:
                        admit.return_value = admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=False,
                        )
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": "attacker-supplied",
                                "password": "wrong-password",
                                "csrf_token": "bad",
                            },
                        )
    assert response.status_code == 400
    assert captured["actor_context"].actor == "anonymous"
    assert "attacker-supplied" not in str(captured)


@pytest.mark.unit
def test_successful_login_retains_administrator_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    captured: dict[str, Any] = {}

    def _capture_success(conn: MagicMock, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        with patch("app.admin_routes.db.create_admin_session", return_value=42):
            with patch("app.admin_routes.db.db_connection") as db_conn:
                conn = MagicMock()
                db_conn.return_value.__enter__.return_value = conn
                with patch(
                    "app.admin_routes.audit_service.record_login_success",
                    side_effect=_capture_success,
                ):
                    with patch("app.admin_auth.try_admit_login_attempt") as admit:
                        admit.return_value = admin_auth.LoginAdmissionResult(
                            admitted=True,
                            throttled=False,
                            already_locked=False,
                            lockout_transition=False,
                        )
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert response.status_code == 303
    assert captured["actor_context"].actor == TEST_USERNAME
    assert captured["session_id"] == 42


@pytest.mark.unit
def test_limiter_logs_exclude_raw_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings()
    request = MagicMock()
    request.cookies = {}
    request.headers = {}
    request.client = MagicMock()
    request.client.host = "203.0.113.77"
    caplog.set_level(logging.INFO)
    with patch("app.admin_auth.db.db_connection", side_effect=RuntimeError("db down")):
        admin_auth.try_admit_login_attempt(
            request,
            settings,
            username="attacker@example.com",
        )
    combined = caplog.text
    assert "attacker@example.com" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113.77" not in combined


@pytest.mark.integration
def test_postgres_rows_store_keyed_identifiers_and_anonymous_actors(pg_conn: Any) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.42", settings)
    plain = _plain_sha256("src", "203.0.113.42")
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain
    assert len(row["limiter_key"]) == 64

    with patch("app.admin_routes.get_settings", return_value=settings):
        with patch("app.admin_routes.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = pg_conn
            with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
                from app.admin_routes import _record_login_failure

                request = MagicMock()
                request.headers = {"x-request-id": "pg-trace"}
                request.state = MagicMock()
                request.state.correlation_id = "pg-trace"
                _record_login_failure(request, reason="invalid_credentials")
                failure_audit.assert_called_once()
                actor = failure_audit.call_args.kwargs["actor_context"].actor
                assert actor == "anonymous"
