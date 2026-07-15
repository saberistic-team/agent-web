"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

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
from app.actor_context import ActorContext
from app.admin_secrets import AdminSecretValidationError, validate_admin_secret_value, validate_admin_security_secrets
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min"
ALT_LIMITER_SECRET = "alt-login-limiter-secret-32chars-minimum"
PREVIOUS_LIMITER_SECRET = "prev-login-limiter-secret-32chars-min"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


@pytest.fixture(autouse=True)
def limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    return Settings(
        database_url=overrides.get("database_url", base.database_url),
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=overrides.get("base_url", base.base_url),
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=overrides.get("admin_username", base.admin_username),
        admin_password_hash=overrides.get("admin_password_hash", base.admin_password_hash),
        admin_session_secret=overrides.get("admin_session_secret", base.admin_session_secret),
        admin_login_limiter_secret=overrides.get(
            "admin_login_limiter_secret", base.admin_login_limiter_secret
        ),
        admin_login_limiter_secret_previous=overrides.get(
            "admin_login_limiter_secret_previous", base.admin_login_limiter_secret_previous
        ),
    )


def _plain_sha256(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_conn,
        patch("app.admin_auth.db.db_connection") as auth_conn,
    ):
        route_conn.return_value.__enter__.return_value = conn
        route_conn.return_value.__exit__.return_value = None
        auth_conn.return_value.__enter__.return_value = conn
        auth_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_record_login_failure_repository_persists_anonymous_actor_only() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-spy"}
    conn = MagicMock()
    audit_service.record_login_failure(
        conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-spy"),
        reason="invalid_credentials",
        repository=repo,
    )
    repo.append.assert_called_once()
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert append_kwargs["action"] == audit_service.ACTION_AUTH_LOGIN_FAILURE
    payload = json.dumps(
        {
            "summary_after": append_kwargs.get("summary_after"),
            "metadata": append_kwargs.get("metadata"),
        }
    )
    assert "ghost-candidate" not in payload
    assert append_kwargs["summary_after"] == {"reason": "invalid_credentials"}


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
    account_key = admin_auth.build_account_rate_limit_key("operator", settings=settings)
    assert source_key != _plain_sha256("src", "203.0.113.10")
    assert account_key != _plain_sha256("acct", "operator")


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    current = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    alternate = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    assert (
        admin_auth.build_source_rate_limit_key("203.0.113.10", settings=current)
        != admin_auth.build_source_rate_limit_key("203.0.113.10", settings=alternate)
    )


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = get_settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = get_settings()
    shared_material = "203.0.113.10"
    source_key = admin_auth.build_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        shared_material,
        settings=settings,
    )
    account_key = admin_auth.build_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_ACCOUNT,
        shared_material,
        settings=settings,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "short", "changeme"])
def test_limiter_secret_validation_rejects_weak_values(value: str) -> None:
    with pytest.raises(AdminSecretValidationError):
        validate_admin_secret_value(value, name="ADMIN_LOGIN_LIMITER_SECRET")


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    with pytest.raises(AdminSecretValidationError, match="must differ"):
        validate_admin_security_secrets(get_settings())


@pytest.mark.unit
def test_rotation_lookup_honors_previous_secret_lockout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    previous_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.10",
        secret=PREVIOUS_LIMITER_SECRET,
    )
    current_key = admin_auth.build_source_rate_limit_key("203.0.113.10", settings=settings)
    assert previous_key != current_key

    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with mock_db_connection():
        with patch(
            "app.admin_auth.db.is_admin_login_throttled",
            side_effect=lambda _conn, *, limiter_key, now: limiter_key == previous_key,
        ):
            request = MagicMock()
            with patch("app.admin_auth.client_ip", return_value="203.0.113.10"):
                assert admin_auth.is_login_throttled(request, settings, username=TEST_USERNAME)


_ADMITTED = admin_auth.LoginAdmissionResult(
    admitted=True,
    throttled=False,
    already_locked=False,
    lockout_transition=False,
)


@pytest.mark.unit
def test_unknown_username_failure_records_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    login = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-candidate",
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert login.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert "attacker-candidate" not in repr(failure_audit.call_args.kwargs)


@pytest.mark.unit
def test_configured_username_wrong_password_actor_remains_anonymous() -> None:
    with mock_db_connection():
        with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    login = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
    assert login.status_code == 401
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_credentials"


@pytest.mark.unit
def test_invalid_csrf_failure_actor_is_anonymous() -> None:
    with mock_db_connection():
        with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
            with patch("app.admin_routes._try_claim_login_flow", return_value=False):
                with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                    with patch(
                        "app.admin_routes.audit_service.record_login_failure"
                    ) as failure_audit:
                        login = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert login.status_code == 400
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
def test_successful_login_retains_authenticated_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success"
                    ) as success_audit:
                        login = client.post(
                            "/admin/login",
                            data={
                                "username": TEST_USERNAME,
                                "password": TEST_PASSWORD,
                                "csrf_token": "flow-csrf",
                            },
                        )
    assert login.status_code == 303
    success_audit.assert_called_once()
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_login_failure_logs_exclude_candidates_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with mock_db_connection():
        with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                candidate = "attacker-candidate"
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
    assert response.status_code == 401
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:203.0.113" not in combined


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@pytest.fixture
def pg_conn() -> Generator[psycopg.Connection, None, None]:
    database_url = _require_database_url()
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
        conn.rollback()
        conn.close()
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings=settings)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
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
    assert row["limiter_key"] != _plain_sha256("src", "203.0.113.88")
    assert len(row["limiter_key"]) == 64

    repo = MagicMock()
    repo.append.side_effect = lambda **kwargs: {
        "id": "evt-pg",
        "actor": kwargs["actor"],
        "summary_after": kwargs.get("summary_after"),
        "metadata": kwargs.get("metadata"),
    }
    actor = audit_service.record_login_failure(
        pg_conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg"),
        reason="invalid_credentials",
        repository=repo,
    )
    assert actor is not None
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert "operator" not in json.dumps(append_kwargs)


@pytest.mark.integration
def test_rotation_previous_key_rows_remain_eligible_for_cleanup(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", PREVIOUS_LIMITER_SECRET)
    previous_key = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_KEY_DOMAIN_SOURCE,
        "203.0.113.99",
        secret=PREVIOUS_LIMITER_SECRET,
    )
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 5, %s, %s, %s)
            """,
            (
                previous_key,
                5,
                stale,
                stale + timedelta(seconds=60),
                stale + timedelta(days=1),
            ),
        )
    pg_conn.commit()

    cleaned = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=stale + timedelta(days=2),
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()
    assert cleaned >= 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (previous_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0
