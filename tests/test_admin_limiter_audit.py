"""Tests for keyed login limiter identifiers and anonymous failure audit actors (#242)."""

from __future__ import annotations

pytest_plugins = ["tests.test_admin_auth"]

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_auth import LOGIN_FLOW_COOKIE_NAME, SESSION_COOKIE_NAME
from app.admin_security import AdminSecurityConfigError, validate_admin_secret
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from psycopg.rows import dict_row
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_LIMITER_SECRET,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

KNOWN_SOURCE = "203.0.113.42"
KNOWN_ACCOUNT = "operator"
ALT_LIMITER_SECRET = "alternate-limiter-secret-32chars-minimum"
PREVIOUS_LIMITER_SECRET = "previous-limiter-secret-32chars-minimum"


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    fields = {
        "database_url": base.database_url,
        "stripe_secret_key": base.stripe_secret_key,
        "stripe_webhook_secret": base.stripe_webhook_secret,
        "stripe_publishable_key": base.stripe_publishable_key,
        "resend_api_key": base.resend_api_key,
        "from_email": base.from_email,
        "notify_email": base.notify_email,
        "base_url": base.base_url,
        "plausible_domain": base.plausible_domain,
        "plausible_api_key": base.plausible_api_key,
        "analytics_environment": base.analytics_environment,
        "admin_username": TEST_USERNAME,
        "admin_password_hash": TEST_HASH,
        "admin_session_secret": TEST_SECRET,
        "admin_login_limiter_secret": TEST_LIMITER_SECRET,
        "admin_login_limiter_previous_secret": "",
    }
    fields.update(overrides)
    return Settings(**fields)


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    payload = f"{prefix}:{material.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def limiter_audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source_key = admin_auth.build_source_rate_limit_key(KNOWN_SOURCE, settings)
    account_key = admin_auth.build_account_rate_limit_key(KNOWN_ACCOUNT, settings)
    assert source_key != _plain_sha256_limiter_key("src", KNOWN_SOURCE)
    assert account_key != _plain_sha256_limiter_key("acct", KNOWN_ACCOUNT)
    assert len(source_key) == 64
    assert len(account_key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(admin_login_limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(admin_login_limiter_secret=ALT_LIMITER_SECRET)
    key_a = admin_auth.build_source_rate_limit_key(KNOWN_SOURCE, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(KNOWN_SOURCE, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key(KNOWN_SOURCE, settings)
    second = admin_auth.build_source_rate_limit_key(KNOWN_SOURCE, settings)
    assert first == second
    assert first == hmac.new(
        TEST_LIMITER_SECRET.encode("utf-8"),
        f"src:{KNOWN_SOURCE.lower()}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    settings = _settings()
    shared_material = "203.0.113.1"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "required"),
        ("short", "at least 32"),
        ("this-is-a-placeholder-secret-value!!", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    message: str,
) -> None:
    with pytest.raises(AdminSecurityConfigError, match=message):
        validate_admin_secret("ADMIN_LOGIN_LIMITER_SECRET", secret)


@pytest.mark.unit
def test_startup_validation_rejects_identical_current_and_previous_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_security import validate_admin_security_settings

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_security_settings(settings)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin_security import validate_admin_security_settings

    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(AdminSecurityConfigError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_settings(settings)


@pytest.mark.unit
def test_rotation_produces_distinct_current_and_legacy_keys() -> None:
    settings = _settings(admin_login_limiter_previous_secret=PREVIOUS_LIMITER_SECRET)
    current = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=KNOWN_SOURCE,
    )
    legacy = admin_auth.login_limiter_legacy_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=KNOWN_SOURCE,
    )
    assert len(current) == 2
    assert len(legacy) == 2
    assert not set(current).intersection(legacy)


class _AuditSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"id": str(len(self.calls))}


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    with mock_db_connection():
        response = client.get("/admin/login")
    assert response.status_code == 200
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    cookies: dict[str, str] = {}
    flow_cookie = response.cookies.get(LOGIN_FLOW_COOKIE_NAME)
    if flow_cookie:
        cookies[LOGIN_FLOW_COOKIE_NAME] = flow_cookie
    return match.group(1), cookies


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str | None = None,
    cookies: dict[str, str] | None = None,
) -> Any:
    if csrf_token is None or cookies is None:
        csrf_token, cookies = _fetch_login_form()
    with mock_db_connection():
        return client.post(
            "/admin/login",
            data={"username": username, "password": password, "csrf_token": csrf_token},
            cookies=cookies,
        )


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor(
    rate_limit_store: Any,
) -> None:
    spy = _AuditSpy()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
                ):
                    response = _login(username="attacker-candidate", password="wrong-password")
    assert response.status_code == 401
    assert len(spy.calls) == 1
    assert spy.calls[0]["actor_context"].actor == "anonymous"
    payload = json.dumps(spy.calls[0], default=lambda o: getattr(o, "__dict__", str(o)))
    assert "attacker-candidate" not in payload


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor(
    rate_limit_store: Any,
) -> None:
    spy = _AuditSpy()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
                ):
                    response = _login(password="wrong-password")
    assert response.status_code == 401
    assert spy.calls[0]["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(spy.calls[0], default=str)


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor(
    rate_limit_store: Any,
) -> None:
    spy = _AuditSpy()
    _csrf_token, cookies = _fetch_login_form()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=spy,
            ):
                response = client.post(
                    "/admin/login",
                    data={
                        "username": TEST_USERNAME,
                        "password": "wrong-password",
                        "csrf_token": "not-the-form-token",
                    },
                    cookies=cookies,
                )
    assert response.status_code == 400
    assert spy.calls
    assert spy.calls[-1]["actor_context"].actor == "anonymous"
    assert spy.calls[-1]["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_uses_anonymous_actor(
    rate_limit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    spy = _AuditSpy()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=spy,
                ):
                    assert _login(password="wrong").status_code == 401
                    assert _login(password="wrong").status_code == 401
    assert any(call["reason"] == "rate_limited" for call in spy.calls)
    assert all(call["actor_context"].actor == "anonymous" for call in spy.calls)


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_administrator_actor(
    rate_limit_store: Any,
) -> None:
    spy = _AuditSpy()
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success",
                        side_effect=spy,
                    ):
                        response = _login()
    assert response.status_code == 303
    assert len(spy.calls) == 1
    assert spy.calls[0]["actor_context"].actor == TEST_USERNAME
    assert isinstance(spy.calls[0]["session_id"], int)


@pytest.mark.unit
@pytest.mark.integration
def test_failed_login_logs_exclude_candidates_and_secrets(
    rate_limit_store: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                response = _login(username="logged-candidate", password="wrong-password")
    assert response.status_code == 401
    combined = caplog.text
    assert "logged-candidate" not in combined
    assert TEST_LIMITER_SECRET not in combined


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifier_and_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (pytest.importorskip("os").environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.88", settings)
    plain = _plain_sha256_limiter_key("src", "203.0.113.88")
    assert source_key != plain

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    try:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (source_key,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["limiter_key"] == source_key
            assert row["limiter_key"] != plain

            audit_service.record_login_failure(
                conn,
                actor_context=ActorContext(actor="anonymous", correlation_id="pg-test"),
                reason="invalid_credentials",
            )
            conn.commit()

        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT actor, summary_after
                    FROM audit_events
                    WHERE action = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (audit_service.ACTION_AUTH_LOGIN_FAILURE,),
                )
                audit_row = cur.fetchone()
            assert audit_row is not None
            assert audit_row["actor"] == "anonymous"
            assert "candidate" not in str(audit_row["summary_after"]).lower()
    finally:
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()


@pytest.mark.integration
def test_legacy_lockout_blocks_admission_during_rotation(
    rate_limit_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", PREVIOUS_LIMITER_SECRET)
    settings = get_settings()
    legacy_keys = admin_auth.login_limiter_legacy_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=KNOWN_SOURCE,
    )
    now = datetime.now(timezone.utc)
    for key in legacy_keys:
        rate_limit_store.rows[key] = {
            "failure_count": 2,
            "window_started_at": now,
            "locked_until": now + timedelta(seconds=900),
            "updated_at": now,
        }

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/admin/login",
        "raw_path": b"/admin/login",
        "query_string": b"",
        "headers": [],
        "client": (KNOWN_SOURCE, 12345),
        "server": ("testserver", 80),
    }
    from starlette.requests import Request

    request = Request(scope)
    with shared_rate_limiter(rate_limit_store):
        admission = admin_auth.try_admit_login_attempt(request, settings, username=TEST_USERNAME)
    assert not admission.admitted
    assert admission.already_locked


@pytest.mark.integration
def test_rotation_cleanup_removes_stale_previous_key_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = (pytest.importorskip("os").environ.get("TEST_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    settings = _settings(admin_login_limiter_previous_secret=PREVIOUS_LIMITER_SECRET)
    legacy_key = admin_auth.login_limiter_legacy_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source=KNOWN_SOURCE,
    )[0]

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    now = datetime(2026, 7, 3, tzinfo=timezone.utc)
    try:
        with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
            db.try_admit_admin_login(
                conn,
                limiter_keys=(legacy_key,),
                now=now,
                rate_limit=5,
                window_seconds=60,
                lockout_seconds=60,
            )
            deleted = db.cleanup_expired_admin_login_rate_limits(
                conn,
                now=now + timedelta(seconds=200),
                window_seconds=60,
                lockout_seconds=60,
            )
            assert deleted >= 1
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM admin_login_rate_limits")
                row = cur.fetchone()
            assert row is not None
            assert int(row["count"]) == 0
    finally:
        with psycopg.connect(database_url, autocommit=False) as cleanup:
            cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cleanup.execute("CREATE SCHEMA public")
            cleanup.commit()
