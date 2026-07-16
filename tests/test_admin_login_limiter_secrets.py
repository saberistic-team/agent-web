"""Tests for keyed admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import logging
import re
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, audit_service, db
from app.admin_security import (
    LIMITER_DOMAIN_ACCOUNT,
    LIMITER_DOMAIN_SOURCE,
    AdminSecurityConfigError,
    digest_limiter_key,
    plain_sha256_limiter_key,
    validate_admin_security_config,
    validate_limiter_secret,
)
from app.config import Settings, get_settings
from app.main import app

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-login-limiter-secret-32chars-min"
TEST_LIMITER_SECRET_ALT = "alt-login-limiter-secret-32chars-min"
TEST_LIMITER_SECRET_PREVIOUS = "prev-login-limiter-secret-32chars-min"

client = TestClient(app, follow_redirects=False)
_login_flows: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_limiter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "5")
    admin_auth.reset_login_rate_limiter()
    _login_flows.clear()


def _mock_create_admin_login_flow(conn: MagicMock, **kwargs: Any) -> int:
    flow_hash = kwargs["flow_token_hash"]
    _login_flows[flow_hash] = {
        "id": len(_login_flows) + 1,
        "flow_token_hash": flow_hash,
        "csrf_token_hash": kwargs["csrf_token_hash"],
        "expires_at": kwargs["expires_at"],
        "consumed_at": None,
    }
    return int(_login_flows[flow_hash]["id"])


def _mock_claim_admin_login_flow(
    conn: MagicMock,
    *,
    flow_token_hash: str,
    csrf_token_hash: str,
    now: datetime,
) -> dict[str, Any] | None:
    row = _login_flows.get(flow_token_hash)
    if row is None or row.get("consumed_at") is not None:
        return None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        return None
    if row["csrf_token_hash"] != csrf_token_hash:
        return None
    row["consumed_at"] = now
    return dict(row)


@contextmanager
def mock_route_db() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with ExitStack() as stack:
        db_conn_patch = stack.enter_context(patch("app.admin_routes.db.db_connection"))
        stack.enter_context(
            patch("app.admin_routes.db.create_admin_login_flow", _mock_create_admin_login_flow)
        )
        stack.enter_context(
            patch("app.admin_routes.db.cleanup_stale_admin_login_flows", return_value=0)
        )
        stack.enter_context(
            patch("app.admin_routes.db.claim_admin_login_flow", _mock_claim_admin_login_flow)
        )
        stack.enter_context(patch("app.admin_routes.db.create_admin_session", return_value=99))
        stack.enter_context(patch("app.admin_routes.db.revoke_admin_session", return_value=False))
        db_conn_patch.return_value.__enter__.return_value = conn
        db_conn_patch.return_value.__exit__.return_value = None
        yield conn


def _settings(**overrides: str) -> Settings:
    base = get_settings()
    if not overrides:
        return base
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
        admin_login_limiter_previous_secret=overrides.get(
            "admin_login_limiter_previous_secret", base.admin_login_limiter_previous_secret
        ),
        admin_session_ttl_seconds=base.admin_session_ttl_seconds,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
        admin_trust_proxy_headers=base.admin_trust_proxy_headers,
        admin_trusted_proxy_cidrs=base.admin_trusted_proxy_cidrs,
        admin_trusted_edge_cidrs=base.admin_trusted_edge_cidrs,
        audit_page_size=base.audit_page_size,
        brief_page_size=base.brief_page_size,
        analytics_ingest_rate_limit=base.analytics_ingest_rate_limit,
        analytics_ingest_rate_window_seconds=base.analytics_ingest_rate_window_seconds,
        analytics_ingest_lockout_seconds=base.analytics_ingest_lockout_seconds,
    )


@pytest.mark.unit
def test_persisted_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    source = "203.0.113.10"
    keyed = admin_auth.build_source_rate_limit_key(source, settings)
    plain = plain_sha256_limiter_key(LIMITER_DOMAIN_SOURCE, source)
    assert keyed != plain
    assert len(keyed) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", keyed)


@pytest.mark.unit
def test_identifier_depends_on_secret() -> None:
    material = "203.0.113.10"
    current = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material=material,
        secret=TEST_LIMITER_SECRET,
    )
    alternate = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material=material,
        secret=TEST_LIMITER_SECRET_ALT,
    )
    assert current != alternate


@pytest.mark.unit
def test_identifier_is_stable_for_same_inputs() -> None:
    material = "operator"
    first = digest_limiter_key(
        domain=LIMITER_DOMAIN_ACCOUNT,
        material=material,
        secret=TEST_LIMITER_SECRET,
    )
    second = digest_limiter_key(
        domain=LIMITER_DOMAIN_ACCOUNT,
        material=material,
        secret=TEST_LIMITER_SECRET,
    )
    assert first == second


@pytest.mark.unit
def test_domain_separation_for_identical_payload() -> None:
    payload = "203.0.113.10"
    source_key = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material=payload,
        secret=TEST_LIMITER_SECRET,
    )
    account_key = digest_limiter_key(
        domain=LIMITER_DOMAIN_ACCOUNT,
        material=payload,
        secret=TEST_LIMITER_SECRET,
    )
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "env_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("placeholder", "ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, env_name: str) -> None:
    with pytest.raises(AdminSecurityConfigError):
        validate_limiter_secret(secret, env_name=env_name, required=True)


@pytest.mark.unit
def test_startup_validation_requires_strong_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "changeme")
    with pytest.raises(AdminSecurityConfigError):
        validate_admin_security_config(get_settings())


@pytest.mark.unit
def test_rotation_honors_previous_secret_lockout() -> None:
    settings = _settings(
        admin_login_limiter_secret=TEST_LIMITER_SECRET,
        admin_login_limiter_previous_secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    previous_key = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material="testclient",
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    current_key = admin_auth.build_source_rate_limit_key("testclient", settings)
    assert previous_key != current_key

    store: dict[str, dict[str, Any]] = {
        previous_key: {
            "failure_count": 5,
            "window_started_at": now,
            "locked_until": now.replace(year=2027),
        }
    }

    def is_throttled(conn: Any, *, limiter_key: str, now: datetime) -> bool:
        row = store.get(limiter_key)
        if row is None:
            return False
        locked_until = row["locked_until"]
        return locked_until is not None and locked_until > now

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
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
    }
    from starlette.requests import Request

    request = Request(scope)
    with patch("app.admin_auth.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        with patch("app.admin_auth.db.is_admin_login_throttled", side_effect=is_throttled):
            assert admin_auth.is_login_throttled(request, settings, username="ghost")


@pytest.mark.unit
def test_rotation_cleanup_removes_expired_previous_key_rows() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expired_previous = digest_limiter_key(
        domain=LIMITER_DOMAIN_SOURCE,
        material="203.0.113.44",
        secret=TEST_LIMITER_SECRET_PREVIOUS,
    )
    cursor = MagicMock()
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    deleted = db.cleanup_expired_admin_login_rate_limits(
        conn,
        now=now,
        window_seconds=900,
        lockout_seconds=900,
    )
    assert deleted == 1
    sql = cursor.execute.call_args.args[0]
    assert "DELETE FROM admin_login_rate_limits" in sql


@contextmanager
def _mock_login_db() -> Generator[MagicMock, None, None]:
    with (
        mock_route_db() as conn,
        patch("app.admin_auth.db.db_connection") as auth_db,
        patch(
            "app.admin_auth.db.try_admit_admin_login",
            return_value=db.AdminLoginAdmission(
                admitted=True,
                throttled=False,
                already_locked=False,
                lockout_transition=False,
            ),
        ),
    ):
        auth_db.return_value.__enter__.return_value = MagicMock()
        auth_db.return_value.__exit__.return_value = None
        yield conn


def _login_form() -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    cookies: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("admin_login_flow="):
            cookies["admin_login_flow"] = header.split("=", 1)[1].split(";", 1)[0]
    return match.group(1), cookies


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_uses_anonymous_actor_only() -> None:
    candidate = "attacker-candidate"

    with (
        _mock_login_db(),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit,
    ):
        csrf_token, cookies = _login_form()
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
    failure_audit.assert_called_once()
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert "attempted_username" not in failure_audit.call_args.kwargs


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_keeps_anonymous_actor() -> None:
    with (
        _mock_login_db(),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit,
    ):
        csrf_token, cookies = _login_form()
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
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_uses_anonymous_actor() -> None:
    with (
        _mock_login_db(),
        patch("app.admin_routes._try_claim_login_flow", return_value=False),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit,
    ):
        csrf_token, cookies = _login_form()
        response = client.post(
            "/admin/login",
            data={
                "username": "ghost-user",
                "password": "wrong-password",
                "csrf_token": csrf_token,
            },
            cookies=cookies,
        )

    assert response.status_code == 400
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "invalid_csrf"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_uses_anonymous_actor() -> None:
    admission = db.AdminLoginAdmission(
        admitted=True,
        throttled=False,
        already_locked=False,
        lockout_transition=True,
    )

    with (
        _mock_login_db(),
        patch("app.admin_auth.db.try_admit_admin_login", return_value=admission),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            wraps=audit_service.record_login_failure,
        ) as failure_audit,
    ):
        csrf_token, cookies = _login_form()
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
    assert failure_audit.call_args.kwargs["actor_context"].actor == "anonymous"
    assert failure_audit.call_args.kwargs["reason"] == "rate_limited"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_retains_authenticated_actor() -> None:
    with (
        mock_route_db(),
        patch("app.admin_auth.db.db_connection") as auth_db,
        patch(
            "app.admin_routes.audit_service.record_login_success",
        ) as success_audit,
    ):
        auth_db.return_value.__enter__.return_value = MagicMock()
        auth_db.return_value.__exit__.return_value = None

        csrf_token, cookies = _login_form()
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
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME


@pytest.mark.unit
def test_logs_do_not_leak_candidates_or_limiter_material(caplog: pytest.LogCaptureFixture) -> None:
    from starlette.requests import Request

    candidate = "leaked-candidate-user"
    source = "203.0.113.77"
    caplog.set_level(logging.INFO)

    settings = _settings()
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
        "client": (source, 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    with caplog.at_level(logging.INFO, logger="app.admin_auth"):
        admin_auth.build_source_rate_limit_key(source, settings)
        admin_auth.try_admit_login_attempt(request, settings, username=candidate)

    combined = caplog.text
    assert candidate not in combined
    assert source not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert f"src:{source}" not in combined
