"""Security tests for keyed admin login limiter identifiers and anonymous audit actors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.config import Settings, get_settings
from app.crm_uow import crm_transaction
from app.main import app
from app.migrations.runner import apply_migrations

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings(**overrides: str) -> Settings:
    env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "ADMIN_USERNAME": TEST_USERNAME,
        "ADMIN_PASSWORD_HASH": TEST_HASH,
        "ADMIN_SESSION_SECRET": TEST_SECRET,
        "ADMIN_LOGIN_LIMITER_SECRET": TEST_LIMITER_SECRET,
        "BASE_URL": "http://testserver",
    }
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return get_settings()


@pytest.fixture(autouse=True)
def limiter_security_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.50"
    keyed = admin_auth.build_source_rate_limit_key(source, settings=settings)
    plain = admin_auth._plain_sha256_limiter_key(admin_auth.LIMITER_DOMAIN_SOURCE, source.lower())
    assert keyed != plain


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET)
    settings_b = _settings(ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET_ALT)
    source = "203.0.113.50"
    key_a = admin_auth.build_source_rate_limit_key(source, settings=settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings=settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.50", settings=settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.50", settings=settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    payload = "operator"
    source_key = admin_auth.build_source_rate_limit_key(payload, settings=settings)
    account_key = admin_auth.build_account_rate_limit_key(payload, settings=settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "secret"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", ""),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "placeholder"),
        ("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", "short"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    env_name: str,
    secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    monkeypatch.setenv(env_name, secret)
    settings = get_settings()
    with pytest.raises(ValueError):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_matching_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_require_admin_auth_configured_fails_on_weak_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from app.admin_routes import _require_admin_auth_configured

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "short")
    settings = get_settings()
    with pytest.raises(HTTPException) as exc_info:
        _require_admin_auth_configured(settings)
    assert exc_info.value.status_code == 503


@pytest.mark.unit
def test_rotation_reconciles_previous_key_rows() -> None:
    settings = _settings(
        ADMIN_LOGIN_LIMITER_SECRET=TEST_LIMITER_SECRET,
        ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS=TEST_LIMITER_SECRET_PREVIOUS,
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    canonical = admin_auth.build_source_rate_limit_key("203.0.113.88", settings=settings)
    legacy = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.88",
        TEST_LIMITER_SECRET_PREVIOUS,
    )

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [
        {
            "limiter_key": legacy,
            "failure_count": 4,
            "window_started_at": now,
            "locked_until": None,
            "updated_at": now,
        },
        None,
    ]

    db.reconcile_admin_login_limiter_aliases(
        conn,
        alias_pairs=((canonical, legacy),),
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )

    executed = " ".join(call.args[0] for call in cur.execute.call_args_list)
    assert "INSERT INTO admin_login_rate_limits" in executed
    assert "DELETE FROM admin_login_rate_limits" in executed


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter tests")


@pytest.mark.integration
def test_pg_limiter_rows_store_hmac_identifiers_and_cleanup_legacy_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET_PREVIOUS)
    settings = get_settings()

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    canonical = admin_auth.build_source_rate_limit_key("203.0.113.99", settings=settings)
    legacy = admin_auth._digest_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.99",
        TEST_LIMITER_SECRET_PREVIOUS,
    )
    plain = admin_auth._plain_sha256_limiter_key(
        admin_auth.LIMITER_DOMAIN_SOURCE,
        "203.0.113.99",
    )
    assert canonical != plain

    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_login_rate_limits (
                    limiter_key, failure_count, window_started_at, locked_until, updated_at
                )
                VALUES (%s, 3, %s, NULL, %s)
                """,
                (legacy, now, now),
            )
        conn.commit()

        db.reconcile_admin_login_limiter_aliases(
            conn,
            alias_pairs=((canonical, legacy),),
            now=now,
            window_seconds=900,
            lockout_seconds=900,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key, failure_count FROM admin_login_rate_limits ORDER BY limiter_key"
            )
            rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["limiter_key"] == canonical
    assert int(rows[0]["failure_count"]) == 3
    assert len(rows[0]["limiter_key"]) == 64


@pytest.fixture
def rate_limit_store() -> Any:
    from tests.test_admin_auth import FakeRateLimitStore

    return FakeRateLimitStore()


@contextmanager
def _mock_db_connection() -> Generator[MagicMock, None, None]:
    from tests.test_admin_auth import mock_db_connection

    with mock_db_connection() as conn:
        yield conn


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    from tests.test_admin_auth import _fetch_login_form as fetch

    return fetch()


def _login(**kwargs: Any) -> Any:
    from tests.test_admin_auth import _login as login

    return login(**kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch("app.audit_service.get_repositories") as get_repos:
                    get_repos.return_value.audit_events = repo
                    response = _login(username="attacker-candidate", password="wrong")
                    assert response.status_code == 401
                    failure_audit.assert_called_once()
                    actor_context = failure_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == "anonymous"
                    repo.append.assert_called_once()
                    append_kwargs = repo.append.call_args.kwargs
                    assert append_kwargs["actor"] == "anonymous"
                    event_json = json.dumps(append_kwargs)
                    assert "attacker-candidate" not in event_json


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_actor_is_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-2"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                with patch("app.audit_service.get_repositories") as get_repos:
                    get_repos.return_value.audit_events = repo
                    response = _login(password="wrong-password")
                    assert response.status_code == 401
                    actor_context = failure_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == "anonymous"
                    append_kwargs = repo.append.call_args.kwargs
                    assert TEST_USERNAME not in json.dumps(append_kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous() -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-3"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ):
                with patch("app.audit_service.get_repositories") as get_repos:
                    get_repos.return_value.audit_events = repo
                    csrf_token, cookies = _fetch_login_form()
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-candidate",
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
                    assert response.status_code == 400
                    append_kwargs = repo.append.call_args.kwargs
                    assert append_kwargs["actor"] == "anonymous"
                    assert "attacker-candidate" not in json.dumps(append_kwargs)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    rate_limit_store: Any,
) -> None:
    from tests.test_admin_auth import shared_rate_limiter

    with shared_rate_limiter(rate_limit_store):
        with _mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    client.cookies.clear()
                    response = _login()
                    assert response.status_code == 303
                    actor_context = success_audit.call_args.kwargs["actor_context"]
                    assert actor_context.actor == TEST_USERNAME
                    assert success_audit.call_args.kwargs["session_id"] is not None


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
    rate_limit_store: Any,
) -> None:
    from tests.test_admin_auth import shared_rate_limiter

    caplog.set_level(logging.INFO)
    candidate = "attacker-candidate@example.com"
    from tests.test_admin_auth import _session_store

    _session_store.clear()
    client.cookies.clear()
    with shared_rate_limiter(rate_limit_store):
        with _mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401

    combined = caplog.text + (response.text or "")
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "test-limiter-secret" not in combined
