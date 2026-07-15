"""Tests for HMAC admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
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

from app import admin_auth, audit_service, db
from app.admin_security import AdminSecurityConfigError, validate_admin_login_limiter_secret
from app.admin_security import validate_admin_login_limiter_settings
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SESSION_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "prod-limiter-secret-32chars-minimum!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum"
TEST_CANDIDATE = "attacker-controlled-candidate@evil.example"


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_secret_previous: str = "",
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
        admin_session_secret=TEST_SESSION_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_secret_previous,
    )


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    admin_auth.reset_login_rate_limiter()


@contextmanager
def _mock_login_db() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_conn,
        patch("app.admin_routes.db.create_admin_login_flow", return_value=1),
        patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0),
        patch("app.admin_routes.db.claim_admin_login_flow", return_value={"id": 1}),
        patch("app.admin_routes.db.consume_admin_login_flow", return_value=False),
        patch("app.admin_auth.db.db_connection") as auth_conn,
    ):
        route_conn.return_value.__enter__.return_value = conn
        route_conn.return_value.__exit__.return_value = None
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        yield conn


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
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_LIMITER_SECRET_PREVIOUS)
    source = "203.0.113.1"
    assert admin_auth.build_source_rate_limit_key(source, settings_a) != (
        admin_auth.build_source_rate_limit_key(source, settings_b)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    source = "203.0.113.1"
    first = admin_auth.build_source_rate_limit_key(source, settings)
    second = admin_auth.build_source_rate_limit_key(source, settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_source_and_account() -> None:
    settings = _settings()
    material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(material, settings)
    account_key = admin_auth.build_account_rate_limit_key(material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short-secret", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme-admin-login-limiter-secret-value", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("replace-me-with-a-long-enough-secret-value", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, env_name: str) -> None:
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_login_limiter_secret(secret, env_name=env_name)


@pytest.mark.unit
def test_limiter_settings_validation_rejects_matching_previous_secret() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_secret_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_login_limiter_settings(settings)


@pytest.mark.unit
def test_startup_validation_runs_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changeme-admin-login-limiter-secret-value")
    with pytest.raises(AdminSecurityConfigError):
        with TestClient(app):
            pass


@pytest.mark.unit
def test_rotation_consultation_includes_previous_secret_keys() -> None:
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    keys = admin_auth.login_limiter_consultation_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    current = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    assert len(keys) == len(current) * 2
    assert set(current).issubset(set(keys))


@pytest.mark.unit
def test_rotation_blocks_on_previous_secret_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(limiter_secret_previous=TEST_LIMITER_SECRET_PREVIOUS)
    previous_keys = admin_auth._previous_only_consultation_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    assert len(previous_keys) == 2
    locked_key = previous_keys[0]
    now = datetime.now(timezone.utc)

    def fake_is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        return limiter_key == locked_key

    scope = {
        "type": "http",
        "headers": [],
        "client": ("203.0.113.10", 12345),
        "method": "POST",
        "path": "/admin/login",
    }
    from starlette.requests import Request

    request = Request(scope)
    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        with patch("app.admin_auth.db.is_admin_login_throttled", side_effect=fake_is_throttled):
            result = admin_auth.try_admit_login_attempt(
                request,
                settings,
                username=TEST_USERNAME,
            )
    assert not result.admitted
    assert result.throttled


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_login_db():
        with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.admin_auth.read_login_flow_token", return_value="flow-token"):
                    with patch("app.admin_routes.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = repo
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_CANDIDATE,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert response.status_code == 401
                        append_kwargs = repo.append.call_args.kwargs
                        assert append_kwargs["actor"] == "anonymous"
                        assert TEST_CANDIDATE not in json.dumps(append_kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_login_db():
        with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.admin_auth.read_login_flow_token", return_value="flow-token"):
                    with patch("app.admin_routes.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = repo
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert response.status_code == 401
                        append_kwargs = repo.append.call_args.kwargs
                        assert append_kwargs["actor"] == "anonymous"
                        assert TEST_USERNAME not in json.dumps(append_kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-3"}
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_login_db():
        with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes.admin_auth.read_login_flow_token", return_value="flow-token"):
                    with patch("app.admin_routes.audit_service.get_repositories") as get_repos:
                        get_repos.return_value.audit_events = repo
                        response = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_CANDIDATE,
                                "password": "wrong-password",
                                "csrf_token": "flow-csrf",
                            },
                        )
                        assert response.status_code == 400
                        append_kwargs = repo.append.call_args.kwargs
                        assert append_kwargs["actor"] == "anonymous"
                        assert append_kwargs["action"] == audit_service.ACTION_AUTH_LOGIN_FAILURE
                        assert append_kwargs["metadata"] == {"reason": "invalid_csrf"}


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_login_db():
        with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.admin_auth.read_login_flow_token", return_value="flow-token"):
                    with patch("app.admin_routes.db.create_admin_session", return_value=42):
                        with patch("app.admin_routes.audit_service.record_login_success") as success_audit:
                            response = client.post(
                                "/admin/login",
                                data={
                                    "username": TEST_USERNAME,
                                    "password": TEST_PASSWORD,
                                    "csrf_token": "flow-csrf",
                                },
                            )
                            assert response.status_code == 303
                            actor_context = success_audit.call_args.kwargs["actor_context"]
                            assert actor_context.actor == TEST_USERNAME


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_do_not_leak_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=False,
    )
    with _mock_login_db():
        with patch("app.admin_routes.admin_auth.try_admit_login_attempt", return_value=admitted):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.admin_auth.read_login_flow_token", return_value="flow-token"):
                    client.post(
                        "/admin/login",
                        data={
                            "username": TEST_CANDIDATE,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    combined = caplog.text
    assert TEST_CANDIDATE not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()
_REQUIRE_DB = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRE_DB:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter key tests")


@pytest.fixture
def pg_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    return get_settings()


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_keys_only(
    pg_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    source = "203.0.113.88"
    limiter_key = admin_auth.build_source_rate_limit_key(source, pg_settings)
    plain = hashlib.sha256(f"src:{source}".encode("utf-8")).hexdigest()
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
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
        assert row["limiter_key"] == limiter_key
        assert row["limiter_key"] != plain
        assert source not in row["limiter_key"]


@pytest.mark.integration
def test_rotation_cleanup_eligible_for_previous_secret_rows(
    pg_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    previous_settings = _settings(limiter_secret=TEST_LIMITER_SECRET_PREVIOUS)
    previous_key = admin_auth.build_source_rate_limit_key("203.0.113.89", previous_settings)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)
        bootstrap.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, now, now),
        )
        bootstrap.commit()

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now + timedelta(seconds=2000),
            window_seconds=60,
            lockout_seconds=60,
        )
        assert deleted >= 1
