"""Security tests for keyed login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
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
from app.admin_auth import LIMITER_DOMAIN_ACCOUNT, LIMITER_DOMAIN_SOURCE
from app.config import get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET_ALT = "alt-limiter-secret-32chars-minimum!!"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum!"

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _plain_sha256_limiter_key(domain: str, material: str) -> str:
    return hashlib.sha256(f"{domain}:{material}".encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


def _settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    previous_secret: str = "",
) -> Any:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", limiter_secret)
    if previous_secret:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", previous_secret)
    else:
        monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    return get_settings()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    source_material = "203.0.113.1"
    account_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(source_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(account_material, settings)

    assert source_key != _plain_sha256_limiter_key("src", source_material)
    assert account_key != _plain_sha256_limiter_key("acct", account_material)
    assert len(source_key) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", source_key)


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_a = _settings(monkeypatch, limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(monkeypatch, limiter_secret=TEST_LIMITER_SECRET_ALT)
    source = "203.0.113.9"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    payload = "shared-material"
    source_key = admin_auth._digest_limiter_key(
        LIMITER_DOMAIN_SOURCE,
        payload,
        settings.admin_login_limiter_secret,
    )
    account_key = admin_auth._digest_limiter_key(
        LIMITER_DOMAIN_ACCOUNT,
        payload,
        settings.admin_login_limiter_secret,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("env_name", "secret_value", "message"),
    [
        ("ADMIN_LOGIN_LIMITER_SECRET", "", "required"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "short-secret", "at least 32"),
        ("ADMIN_LOGIN_LIMITER_SECRET", "x" * 32 + "changeme", "placeholder"),
        ("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", "tiny", "at least 32"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    secret_value: str,
    message: str,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", raising=False)
    if env_name == "ADMIN_LOGIN_LIMITER_SECRET":
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", secret_value)
    else:
        monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", secret_value)
    settings = get_settings()
    with pytest.raises(ValueError, match=message):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_limiter_secret_validation_rejects_identical_rotation_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(ValueError, match="must differ"):
        admin_auth.validate_admin_login_limiter_secrets(settings)


@pytest.mark.unit
def test_rotation_lock_check_includes_previous_secret_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    write_keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
    )
    lock_keys = admin_auth.login_limiter_lock_check_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
    )
    assert len(write_keys) == 2
    assert len(lock_keys) == 4
    previous_only = [
        admin_auth._source_limiter_key("203.0.113.1", secret=TEST_LIMITER_SECRET_PREVIOUS),
        admin_auth._account_limiter_key(TEST_USERNAME, secret=TEST_LIMITER_SECRET_PREVIOUS),
    ]
    for key in previous_only:
        assert key in lock_keys
        assert key not in write_keys


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter security tests")


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
def test_rotation_previous_key_blocks_admission_without_incrementing_current(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        limiter_secret=TEST_LIMITER_SECRET,
        previous_secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    previous_source = admin_auth._source_limiter_key(
        "203.0.113.88",
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    current_source = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)

    for index in range(5):
        admission = db.try_admit_admin_login(
            pg_conn,
            limiter_keys=(previous_source,),
            now=now + timedelta(seconds=index),
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

    write_keys = (current_source,)
    lock_keys = (current_source, previous_source)
    blocked = db.try_admit_admin_login(
        pg_conn,
        limiter_keys=write_keys,
        lock_check_keys=lock_keys,
        now=now + timedelta(seconds=10),
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert not blocked.admitted
    assert blocked.already_locked

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT failure_count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (current_source,),
        )
        row = cur.fetchone()
    assert row is None


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_secret_rows(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    stale_key = admin_auth._source_limiter_key("203.0.113.77", secret=TEST_LIMITER_SECRET_PREVIOUS)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(stale_key,),
        now=now,
        rate_limit=5,
        window_seconds=60,
        lockout_seconds=60,
    )
    deleted = db.cleanup_expired_admin_login_rate_limits(
        pg_conn,
        now=now + timedelta(seconds=200),
        window_seconds=60,
        lockout_seconds=60,
    )
    assert deleted >= 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM admin_login_rate_limits WHERE limiter_key = %s",
            (stale_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


@contextmanager
def _mock_db_connection() -> Generator[MagicMock, None, None]:
    mock_conn = MagicMock()
    with (
        patch("app.admin_routes.db.db_connection") as route_db,
        patch("app.db.db_connection") as core_db,
    ):
        route_db.return_value.__enter__.return_value = mock_conn
        route_db.return_value.__exit__.return_value = None
        core_db.return_value.__enter__.return_value = mock_conn
        core_db.return_value.__exit__.return_value = None
        yield mock_conn


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    cookies = {
        cookie.split("=", 1)[0]: cookie.split("=", 1)[1].split(";", 1)[0]
        for cookie in response.headers.get_list("set-cookie")
    }
    return match.group(1), cookies


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_unknown_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _spy(
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> None:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        captured["repository"] = repository

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.audit_service.record_login_failure", side_effect=_spy):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "ghost-candidate",
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401
    assert captured["actor"] == "anonymous"
    assert captured["reason"] == "invalid_credentials"
    assert "ghost-candidate" not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_audit_uses_anonymous_actor_for_configured_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-1"}

    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                wraps=audit_service.record_login_failure,
            ) as failure_audit:
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401
                failure_audit.assert_called_once()
                actor = failure_audit.call_args.kwargs["actor_context"].actor
                assert actor == "anonymous"
                repo.append.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch("app.admin_routes._try_burn_login_flow_cookie", return_value=True):
                with patch("app.admin_routes.audit_service.record_login_failure") as failure_audit:
                    csrf_token, cookies = _fetch_login_form()
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": "attacker-supplied",
                            "password": TEST_PASSWORD,
                            "csrf_token": "wrong-csrf",
                        },
                        cookies=cookies,
                    )
                    assert response.status_code == 400
                    failure_audit.assert_called_once()
                    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
                    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch("app.admin_routes.db.create_admin_session", return_value=42):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    csrf_token, cookies = _fetch_login_form()
                    response = client.post(
                        "/admin/login",
                        data={
                            "username": TEST_USERNAME,
                            "password": TEST_PASSWORD,
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
                    assert response.status_code == 303
                    success_audit.assert_called_once()
                    assert (
                        success_audit.call_args.kwargs["actor_context"].actor
                        == TEST_USERNAME
                    )
                    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
def test_failed_login_logs_do_not_leak_candidates_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "leak-candidate-user"
    with caplog.at_level(logging.INFO):
        with _mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": candidate,
                        "password": "wrong-password",
                        "csrf_token": csrf_token,
                    },
                    cookies=cookies,
                )
                assert response.status_code == 401
    combined = caplog.text
    assert candidate not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert TEST_SECRET not in combined


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted = admin_auth.LoginAdmissionResult(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=True,
    )
    with _mock_db_connection():
        with patch(
            "app.admin_routes.admin_auth.try_admit_login_attempt",
            return_value=admitted,
        ):
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure"
                ) as failure_audit:
                    csrf_token, cookies = _fetch_login_form()
                    client.post(
                        "/admin/login",
                        data={
                            "username": "throttle-candidate",
                            "password": "wrong-password",
                            "csrf_token": csrf_token,
                        },
                        cookies=cookies,
                    )
                    failure_audit.assert_called_once()
                    assert (
                        failure_audit.call_args.kwargs["actor_context"].actor
                        == "anonymous"
                    )
                    assert failure_audit.call_args.kwargs["reason"] == "rate_limited"
                    assert "throttle-candidate" not in str(
                        failure_audit.call_args.kwargs
                    )


@pytest.mark.unit
def test_lifespan_validates_limiter_secret_when_admin_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.main import app as fastapi_app, lifespan

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "short-secret")

    async def _run() -> None:
        async with lifespan(fastapi_app):
            pass

    with pytest.raises(ValueError, match="at least 32"):
        asyncio.run(_run())


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_rows_and_anonymous_failure_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    source = "203.0.113.200"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = _plain_sha256_limiter_key("src", source)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT limiter_key
            FROM admin_login_rate_limits
            WHERE limiter_key = %s
            """,
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert row["limiter_key"] != plain
    assert re.fullmatch(r"[0-9a-f]{64}", row["limiter_key"])

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-pg"}
    from app.actor_context import anonymous_actor_context
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "method": "POST", "path": "/admin/login"}
    request = Request(scope)
    audit_service.record_login_failure(
        pg_conn,
        actor_context=anonymous_actor_context(request),
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert "invalid_credentials" in str(append_kwargs.get("metadata", {}))
    assert TEST_USERNAME not in str(append_kwargs)
