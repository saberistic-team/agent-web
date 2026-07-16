"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
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

from app import admin_auth, db
from app.admin_security import (
    AdminSecurityConfigError,
    validate_admin_security_config,
)
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"
TEST_CLIENT_SOURCE = "203.0.113.42"
TEST_CANDIDATE = "ghost-attacker"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    previous_secret: str = "",
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
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_previous_secret=previous_secret,
    )


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_conn,
        patch("app.admin_auth.db.db_connection") as auth_conn,
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes.db.revoke_admin_session", return_value=True),
    ):
        route_conn.return_value.__enter__.return_value = conn
        route_conn.return_value.__exit__.return_value = None
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        yield conn


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


@pytest.fixture(scope="module")
def database_url() -> str:
    return _require_database_url()


@contextmanager
def _connect(database_url: str) -> Generator[psycopg.Connection, None, None]:
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
def pg_conn(database_url: str) -> Generator[psycopg.Connection, None, None]:
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


@pytest.mark.unit
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    keyed = admin_auth.build_source_rate_limit_key(TEST_CLIENT_SOURCE, settings)
    plain = admin_auth.plain_sha256_limiter_digest("src", TEST_CLIENT_SOURCE.lower())
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_PREVIOUS_LIMITER_SECRET)
    key_a = admin_auth.build_source_rate_limit_key(TEST_CLIENT_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(TEST_CLIENT_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    second = admin_auth.build_account_rate_limit_key(TEST_USERNAME, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_identical_payload() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET is required"),
        ("short-secret", "must be at least 32 characters"),
        ("changeme" * 4, "placeholder"),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    message: str,
) -> None:
    settings = _settings(limiter_secret=secret)
    with pytest.raises(AdminSecurityConfigError, match=message):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_previous_limiter_secret_validation_is_optional() -> None:
    settings = _settings(previous_secret="")
    validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_lookup_includes_previous_secret_keys() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_PREVIOUS_LIMITER_SECRET,
    )
    lookup_keys = admin_auth.login_limiter_keys_for_lookup(
        submitted_username=TEST_USERNAME,
        client_source=TEST_CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    current_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source=TEST_CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    previous_only = admin_auth._limiter_keys_for_secret(
        submitted_username=TEST_USERNAME,
        client_source=TEST_CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        secret=TEST_PREVIOUS_LIMITER_SECRET,
    )
    assert len(lookup_keys) == 4
    assert set(current_keys).issubset(set(lookup_keys))
    assert set(previous_only).issubset(set(lookup_keys))


@pytest.mark.unit
def test_rotation_honors_previous_lockout_without_double_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_keys = admin_auth._limiter_keys_for_secret(
        submitted_username=TEST_USERNAME,
        client_source="testclient",
        configured_admin_username=TEST_USERNAME,
        secret=TEST_PREVIOUS_LIMITER_SECRET,
    )
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    locked_until = now + timedelta(minutes=15)

    def fake_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        return limiter_key == previous_keys[0]

    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        with patch("app.admin_auth.db.is_admin_login_throttled", side_effect=fake_throttled):
            with patch("app.admin_auth.db.try_admit_admin_login") as admit:
                from starlette.requests import Request

                request = Request(
                    {
                        "type": "http",
                        "headers": [],
                        "client": ("testclient", 12345),
                        "method": "POST",
                        "path": "/admin/login",
                    }
                )
                result = admin_auth.try_admit_login_attempt(
                    request, settings, username=TEST_USERNAME
                )
                assert not result.admitted
                assert result.throttled
                admit.assert_not_called()


@pytest.mark.integration
def test_pg_rows_store_keyed_identifiers_and_cleanup_old_secret_rows(
    pg_conn: psycopg.Connection,
) -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_PREVIOUS_LIMITER_SECRET,
    )
    previous_key = admin_auth._limiter_keys_for_secret(
        submitted_username="",
        client_source=TEST_CLIENT_SOURCE,
        configured_admin_username=TEST_USERNAME,
        secret=TEST_PREVIOUS_LIMITER_SECRET,
    )[0]
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_key,),
        now=now - timedelta(hours=2),
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    current_key = admin_auth.build_source_rate_limit_key(TEST_CLIENT_SOURCE, settings)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(current_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits ORDER BY limiter_key"
        )
        rows = [str(row["limiter_key"]) for row in cur.fetchall()]

    assert previous_key in rows
    assert current_key in rows
    assert current_key != admin_auth.plain_sha256_limiter_digest(
        "src", TEST_CLIENT_SOURCE.lower()
    )
    for stored_key in rows:
        assert stored_key not in {TEST_CLIENT_SOURCE, TEST_USERNAME, TEST_CANDIDATE}
        assert len(stored_key) == 64

    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now,
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor_only() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch("app.audit_service.get_repositories") as repos:
                    repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_CANDIDATE,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    repo.append.assert_called_once()
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"
                    serialized = str(repo.append.call_args.kwargs)
                    assert TEST_CANDIDATE not in serialized


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch("app.audit_service.get_repositories") as repos:
                    repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_remains_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-csrf"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch("app.audit_service.get_repositories") as repos:
                    repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_CANDIDATE,
                            "password": TEST_PASSWORD,
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 400
                    assert repo.append.call_args.kwargs["actor"] == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
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
                        assert (
                            success_audit.call_args.kwargs["actor_context"].actor
                            == TEST_USERNAME
                        )
                        assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_exclude_candidates_sources_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.admin_auth.try_admit_login_attempt",
                return_value=admitted,
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=RuntimeError("audit store failure"),
                ):
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_CANDIDATE,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401

    combined = caplog.text
    assert TEST_CANDIDATE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined
