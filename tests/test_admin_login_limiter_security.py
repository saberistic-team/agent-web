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

from app import admin_auth, audit_service, db
from app.admin_security import validate_admin_login_limiter_secret, validate_admin_security_config
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
OTHER_LIMITER_SECRET = "other-limiter-secret-32chars-minimum!!"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-min!!"
CANDIDATE_USERNAME = "attacker-candidate-user"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_limiter_key(domain: str, material: str) -> str:
    payload = f"{domain}:{material}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings_with_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
    *,
    secret: str = TEST_LIMITER_SECRET,
    previous: str = "",
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", secret)
    if previous:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", previous)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    return get_settings()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_with_limiter_secret(monkeypatch)
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_db,
        patch("app.admin_auth.db.db_connection") as auth_db,
    ):
        route_db.return_value.__enter__.return_value = conn
        route_db.return_value.__exit__.return_value = None
        auth_db.return_value.__enter__.return_value = conn
        auth_db.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_limiter_secret(monkeypatch)
    material = "203.0.113.10"
    keyed = admin_auth.build_source_rate_limit_key(settings, material)
    plain = _plain_sha256_limiter_key(admin_auth.LIMITER_DOMAIN_SOURCE, material.lower())
    assert keyed != plain


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = _settings_with_limiter_secret(monkeypatch, secret=TEST_LIMITER_SECRET)
    settings_b = _settings_with_limiter_secret(monkeypatch, secret=OTHER_LIMITER_SECRET)
    material = "203.0.113.10"
    key_a = admin_auth.build_source_rate_limit_key(settings_a, material)
    key_b = admin_auth.build_source_rate_limit_key(settings_b, material)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_limiter_secret(monkeypatch)
    material = "203.0.113.10"
    first = admin_auth.build_source_rate_limit_key(settings, material)
    second = admin_auth.build_source_rate_limit_key(get_settings(), material)
    assert first == second


@pytest.mark.unit
def test_limiter_identifier_domain_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_limiter_secret(monkeypatch)
    payload = "203.0.113.10"
    source_key = admin_auth.build_source_rate_limit_key(settings, payload)
    account_key = admin_auth.build_account_rate_limit_key(settings, payload)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("replace-me-with-a-real-secret-value", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        validate_admin_login_limiter_secret(secret, field_name=field)


@pytest.mark.unit
def test_limiter_secret_validation_accepts_strong_secret() -> None:
    validate_admin_login_limiter_secret(TEST_LIMITER_SECRET)


@pytest.mark.unit
def test_startup_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_limiter_secret(
        monkeypatch,
        secret=TEST_LIMITER_SECRET,
        previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_guard_keys_include_previous_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_limiter_secret(
        monkeypatch,
        secret=TEST_LIMITER_SECRET,
        previous=PREVIOUS_LIMITER_SECRET,
    )
    guard_keys = admin_auth.login_limiter_guard_keys(
        settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    admit_keys = admin_auth.login_limiter_keys(
        settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
    )
    assert len(guard_keys) == 4
    assert len(admit_keys) == 2
    previous_only = admin_auth._hmac_limiter_digest(
        PREVIOUS_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.10",
    )
    assert previous_only in guard_keys
    assert previous_only not in admit_keys


@pytest.mark.unit
def test_rotation_previous_key_lockout_blocks_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_limiter_secret(
        monkeypatch,
        secret=TEST_LIMITER_SECRET,
        previous=PREVIOUS_LIMITER_SECRET,
    )
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/admin/login",
        "client": ("testclient", 0),
    }
    request = Request(scope)
    client_source = admin_auth.client_ip(request, settings)
    previous_source = admin_auth._hmac_limiter_digest(
        PREVIOUS_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        client_source.strip().lower(),
    )
    now = datetime.now(timezone.utc)

    class _Store:
        rows: dict[str, dict[str, Any]] = {}

        @staticmethod
        def is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
            row = _Store.rows.get(limiter_key)
            if row is None:
                return False
            locked_until = row.get("locked_until")
            return locked_until is not None and locked_until > now

        @staticmethod
        def try_admit(*_args: Any, **_kwargs: Any) -> db.AdminLoginAdmission:
            raise AssertionError("admit should not run when previous key is locked")

    _Store.rows = {
        previous_source: {
            "locked_until": now + timedelta(minutes=15),
        }
    }

    with (
        patch("app.admin_auth.db.is_admin_login_throttled", side_effect=_Store.is_throttled),
        patch("app.admin_auth.db.try_admit_admin_login", side_effect=_Store.try_admit),
        patch("app.admin_auth.db.db_connection") as db_conn,
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        result = admin_auth.try_admit_login_attempt(
            request,
            settings,
            username=TEST_USERNAME,
        )
    assert not result.admitted
    assert result.already_locked


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres login limiter tests")


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_key_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = _require_database_url()
    settings = _settings_with_limiter_secret(
        monkeypatch,
        secret=TEST_LIMITER_SECRET,
        previous=PREVIOUS_LIMITER_SECRET,
    )
    previous_key = admin_auth._hmac_limiter_digest(
        PREVIOUS_LIMITER_SECRET,
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.88",
    )
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        conn.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            ) VALUES (%s, 1, %s, NULL, %s)
            """,
            (previous_key, now - timedelta(hours=2), now - timedelta(hours=2)),
        )
        conn.commit()
        deleted = db.cleanup_expired_admin_login_rate_limits(
            conn,
            now=now,
            window_seconds=60,
            lockout_seconds=60,
        )
        assert deleted >= 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
                (previous_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row["count"]) == 0


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_uses_anonymous_actor() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-failure"}
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch.object(
                    audit_service,
                    "get_repositories",
                ) as get_repos:
                    get_repos.return_value.audit_events = repo
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 401
                    failure_audit.assert_called_once()
                    actor_context = failure_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == "anonymous"
                    append_kwargs = repo.append.call_args.kwargs
                    event = {
                        "actor": append_kwargs.get("actor"),
                        "metadata": append_kwargs.get("metadata"),
                        "summary_after": append_kwargs.get("summary_after"),
                    }
                    serialized = str(event)
                    assert CANDIDATE_USERNAME not in serialized


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure"
            ) as failure_audit:
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
                failure_audit.assert_called_once()
                actor_context = failure_audit.call_args.kwargs["actor_context"]
                assert actor_context.actor == "anonymous"
                assert TEST_USERNAME not in str(failure_audit.call_args.kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_uses_anonymous_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=None):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": CANDIDATE_USERNAME,
                            "password": "wrong-password",
                            "csrf_token": "flow-csrf",
                        },
                    )
                    assert response.status_code == 400
                    failure_audit.assert_called_once()
                    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"
                    assert (
                        failure_audit.call_args.kwargs["actor_context"].actor
                        == "anonymous"
                    )


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor() -> None:
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
                    actor_context = success_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == TEST_USERNAME
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_exclude_candidate_and_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=RuntimeError("audit store failure"),
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": CANDIDATE_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401
    log_blob = caplog.text
    assert CANDIDATE_USERNAME not in log_blob
    assert TEST_LIMITER_SECRET not in log_blob
    assert "203.0.113" not in log_blob


@pytest.mark.integration
def test_postgres_limiter_rows_store_keyed_identifiers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    settings = _settings_with_limiter_secret(monkeypatch)
    source = "203.0.113.42"
    keyed = admin_auth.build_source_rate_limit_key(settings, source)
    plain = _plain_sha256_limiter_key(admin_auth.LIMITER_DOMAIN_SOURCE, source)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        db.try_admit_admin_login(
            conn,
            limiter_keys=(keyed,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT limiter_key FROM admin_login_rate_limits")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["limiter_key"] == keyed
        assert rows[0]["limiter_key"] != plain
        assert len(rows[0]["limiter_key"]) == 64


@pytest.mark.integration
def test_postgres_login_failure_audit_actor_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    _settings_with_limiter_secret(monkeypatch)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    repo = MagicMock()
    captured: dict[str, Any] = {}

    def _append(conn, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "evt-1"}

    repo.append.side_effect = _append

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch.object(audit_service, "get_repositories") as get_repos:
                get_repos.return_value.audit_events = repo
                response = client.post(
                    "/admin/login",
                    data={
                        "username": CANDIDATE_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "flow-csrf",
                    },
                )
                assert response.status_code == 401

    assert captured["actor"] == "anonymous"
    assert CANDIDATE_USERNAME not in str(captured)


@pytest.mark.unit
def test_lifespan_validates_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changeme")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        with TestClient(app):
            pass
