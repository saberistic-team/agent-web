"""Tests for HMAC-SHA256 admin login limiter identifiers and anonymous audit actors."""

from __future__ import annotations

import hashlib
import json
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
from app.actor_context import anonymous_actor_context
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_PREVIOUS = "prev-limiter-secret-32chars-minimum!!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings_with_limiter_secret(
    secret: str,
    *,
    previous: str = "",
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
        admin_login_limiter_secret=secret,
        admin_login_limiter_previous_secret=previous,
    )


def _plain_sha256_identifier(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as admin_conn,
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows"),
    ):
        admin_conn.return_value.__enter__.return_value = conn
        admin_conn.return_value.__exit__.return_value = None
        yield conn


@contextmanager
def _admitted_login_attempt(*, lockout_transition: bool = False) -> Iterator[None]:
    admission = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=lockout_transition,
    )
    with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admission):
        yield


@contextmanager
def _mock_claimed_login_flow() -> Iterator[None]:
    with patch("app.admin_routes._try_claim_login_flow", return_value=True):
        yield


def _assert_anonymous_failure_audit(failure_audit: MagicMock, *, reason: str) -> None:
    failure_audit.assert_called_once()
    kwargs = failure_audit.call_args.kwargs
    assert kwargs["actor_context"].actor == "anonymous"
    assert kwargs["reason"] == reason


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256_digest() -> None:
    settings = get_settings()
    source = "203.0.113.10"
    account = TEST_USERNAME
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    account_key = admin_auth.build_account_rate_limit_key(account, settings)

    assert source_key != _plain_sha256_identifier("src", source.lower())
    assert account_key != _plain_sha256_identifier("acct", account.lower())
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_limiter_secret(TEST_LIMITER_SECRET)
    settings_b = _settings_with_limiter_secret(TEST_LIMITER_SECRET_ALT)
    source = "203.0.113.10"

    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_secret_and_input() -> None:
    settings = _settings_with_limiter_secret(TEST_LIMITER_SECRET)
    source = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_separates_source_and_account_domains() -> None:
    settings = get_settings()
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "previous", "message"),
    [
        ("", "", "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short-secret", "", "must be at least 32 characters"),
        ("changeme-" + "x" * 24, "", "must not use placeholder key material"),
        (
            TEST_LIMITER_SECRET,
            "tiny",
            "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET must be at least 32 characters",
        ),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    previous: str,
    message: str,
) -> None:
    settings = _settings_with_limiter_secret(secret, previous=previous)
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_security_settings(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_variants() -> None:
    settings = _settings_with_limiter_secret(
        TEST_LIMITER_SECRET,
        previous=TEST_LIMITER_PREVIOUS,
    )
    keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.10", settings)
    previous_source = admin_auth._digest_limiter_key(
        "src",
        "203.0.113.10",
        TEST_LIMITER_PREVIOUS.encode("utf-8"),
    )
    current_account = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    previous_account = admin_auth._digest_limiter_key(
        "acct",
        TEST_USERNAME.lower(),
        TEST_LIMITER_PREVIOUS.encode("utf-8"),
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
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@contextmanager
def _pg_conn(database_url: str) -> Iterator[psycopg.Connection]:
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
def test_rotation_previous_key_rows_remain_eligible_for_cleanup(pg_conn: psycopg.Connection) -> None:
    previous_key = admin_auth._digest_limiter_key(
        "src",
        "203.0.113.88",
        TEST_LIMITER_PREVIOUS.encode("utf-8"),
    )
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits
                (limiter_key, failure_count, window_started_at, locked_until, updated_at)
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, now - timedelta(seconds=400), now - timedelta(seconds=400)),
        )
    pg_conn.commit()

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@pytest.mark.integration
def test_pg_persisted_limiter_rows_use_hmac_identifiers(pg_conn: psycopg.Connection) -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.42", settings)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    admission = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert admission.admitted
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != _plain_sha256_identifier("src", "203.0.113.42")
    assert len(row["limiter_key"]) == 64


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    candidate = "ghost-attacker@example.com"
    with (
        mock_db_connection(),
        _admitted_login_attempt(),
        _mock_claimed_login_flow(),
        patch("app.admin_routes.audit_service.record_login_failure") as failure_audit,
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": candidate,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )
    assert response.status_code == 401
    _assert_anonymous_failure_audit(failure_audit, reason="invalid_credentials")
    assert candidate not in json.dumps(failure_audit.call_args.kwargs, default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_uses_anonymous_actor() -> None:
    with (
        mock_db_connection(),
        _admitted_login_attempt(),
        _mock_claimed_login_flow(),
        patch("app.admin_routes.audit_service.record_login_failure") as failure_audit,
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )
    assert response.status_code == 401
    _assert_anonymous_failure_audit(failure_audit, reason="invalid_credentials")


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor() -> None:
    with (
        mock_db_connection(),
        _admitted_login_attempt(),
        patch("app.admin_routes._try_claim_login_flow", return_value=False),
        patch("app.admin_routes.audit_service.record_login_failure") as failure_audit,
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )
    assert response.status_code == 400
    _assert_anonymous_failure_audit(failure_audit, reason="invalid_csrf")


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_failure_audit_uses_anonymous_actor() -> None:
    with (
        mock_db_connection(),
        _admitted_login_attempt(lockout_transition=True),
        _mock_claimed_login_flow(),
        patch("app.admin_routes.audit_service.record_login_failure") as failure_audit,
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )
    assert response.status_code == 401
    _assert_anonymous_failure_audit(failure_audit, reason="rate_limited")


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_authenticated_actor() -> None:
    with (
        mock_db_connection(),
        _admitted_login_attempt(),
        _mock_claimed_login_flow(),
        patch("app.admin_routes.db.create_admin_session", return_value=42),
        patch("app.admin_routes.audit_service.record_login_success") as success_audit,
    ):
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )
    assert response.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidate_and_limiter_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "probe-user@evil.example"
    with (
        mock_db_connection(),
        _admitted_login_attempt(),
        _mock_claimed_login_flow(),
        patch("app.admin_routes.audit_service.record_login_failure"),
        caplog.at_level("INFO"),
    ):
        client.post(
            "/admin/login",
            data={
                "username": candidate,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
            cookies={LOGIN_FLOW_COOKIE_NAME: "flow-token"},
        )

    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "203.0.113." not in combined


@pytest.mark.unit
def test_audit_login_failure_repository_spy_persists_anonymous_actor() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    request = MagicMock()
    request.headers = {}
    request.state = MagicMock()
    request.state.correlation_id = "trace-1"
    actor_context = anonymous_actor_context(request)
    audit_service.record_login_failure(
        conn,
        actor_context=actor_context,
        reason="invalid_credentials",
        repository=repo,
    )
    repo.append.assert_called_once()
    assert repo.append.call_args.kwargs["actor"] == "anonymous"
    assert repo.append.call_args.kwargs["summary_after"]["reason"] == "invalid_credentials"
