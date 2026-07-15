"""Tests for keyed admin login limiter identifiers and anonymous failure actors (#242)."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.actor_context import ActorContext
from app import admin_auth, audit_service, db
from app.admin_security import validate_admin_security_config, weak_secret_reason
from app.config import Settings, get_settings
from app.main import app
from app.migrations.runner import apply_migrations
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_HASH,
    TEST_PASSWORD,
    TEST_SECRET,
    TEST_USERNAME,
    FakeRateLimitStore,
    _extract_csrf_token,
    mock_db_connection,
    shared_rate_limiter,
)

client = TestClient(app, follow_redirects=False)

SECRET_A = TEST_LIMITER_SECRET
SECRET_B = "rotation-limiter-secret-32chars-minimum"
SECRET_C = "alternate-limiter-secret-32chars-minimum"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.fixture
def rate_limit_store() -> FakeRateLimitStore:
    return FakeRateLimitStore()

_REQUIRED = (os.environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {"1", "true", "yes"}
_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()


def _settings_with_secrets(
    *,
    current: str = SECRET_A,
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
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=current,
        admin_login_limiter_previous_secret=previous,
    )


def _login(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    csrf_token: str = "flow-csrf",
    cookies: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        cookies=cookies or {},
    )


def _fetch_login_form() -> tuple[str, dict[str, str]]:
    response = client.get("/admin/login")
    assert response.status_code == 200
    return _extract_csrf_token(response.text), response.cookies


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings_with_secrets()
    source_material = "203.0.113.50"
    account_material = "operator"

    source_key = admin_auth.build_source_rate_limit_key(source_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(account_material, settings)

    assert source_key != admin_auth.plain_sha256_limiter_digest("src", source_material)
    assert account_key != admin_auth.plain_sha256_limiter_digest("acct", account_material)


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secrets(current=SECRET_A)
    settings_b = _settings_with_secrets(current=SECRET_B)
    material = "203.0.113.50"

    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_secret() -> None:
    settings = _settings_with_secrets()
    material = "203.0.113.50"

    first = admin_auth.build_source_rate_limit_key(material, settings)
    second = admin_auth.build_source_rate_limit_key(material, settings)
    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_limiter_domain_separation_for_source_and_account() -> None:
    settings = _settings_with_secrets()
    shared_material = "203.0.113.50"

    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "expected_reason"),
    [
        ("", "missing"),
        ("short-secret", "shorter than 32 characters"),
        ("a" * 32, "repeated single character"),
        ("your-secret-thirty-two-characters!!", "placeholder value"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    expected_reason: str,
) -> None:
    assert weak_secret_reason(secret) == expected_reason


@pytest.mark.unit
def test_startup_validation_rejects_missing_limiter_secret() -> None:
    settings = _settings_with_secrets(current="")
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_startup_validation_rejects_matching_rotation_secrets() -> None:
    settings = _settings_with_secrets(current=SECRET_A, previous=SECRET_A)
    with pytest.raises(ValueError, match="must differ"):
        validate_admin_security_config(settings)


@pytest.mark.unit
def test_rotation_lookup_includes_previous_secret_keys() -> None:
    settings = _settings_with_secrets(current=SECRET_A, previous=SECRET_B)
    lookup = admin_auth.login_limiter_lookup_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )
    write_keys = admin_auth.login_limiter_keys(
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.10",
        configured_admin_username=TEST_USERNAME,
        settings=settings,
    )

    previous_source = admin_auth._digest_limiter_key("src", "203.0.113.10", SECRET_B)
    previous_account = admin_auth._digest_limiter_key("acct", TEST_USERNAME, SECRET_B)
    assert previous_source in lookup
    assert previous_account in lookup
    assert previous_source not in write_keys
    assert previous_account not in write_keys


@pytest.mark.unit
def test_rotation_honors_previous_secret_lockout(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", SECRET_C)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET", SECRET_B)
    previous_source = admin_auth._digest_limiter_key("src", "testclient", SECRET_B)
    now = datetime.now(timezone.utc)
    rate_limit_store.rows[previous_source] = {
        "failure_count": 5,
        "window_started_at": now,
        "locked_until": now.replace(year=now.year + 1),
        "updated_at": now,
    }

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            csrf_token, cookies = _fetch_login_form()
            response = _login(csrf_token=csrf_token, cookies=cookies)
    assert response.status_code == 429


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


@pytest.mark.integration
def test_rotation_cleanup_removes_previous_secret_rows() -> None:
    database_url = _require_database_url()
    settings = _settings_with_secrets(current=SECRET_C, previous=SECRET_B)
    stale_key = admin_auth._digest_limiter_key("src", "203.0.113.88", SECRET_B)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    stale_time = now - timedelta(seconds=200)
    with _pg_conn(database_url) as conn:
        conn.execute(
            """
            INSERT INTO admin_login_rate_limits (
                limiter_key, failure_count, window_started_at, locked_until, updated_at
            )
            VALUES (%s, 1, %s, NULL, %s)
            """,
            (stale_key, stale_time, stale_time),
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
                (stale_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row["count"]) == 0


@pytest.mark.unit
@pytest.mark.integration
def test_unknown_username_failure_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = _login(username="ghost-user", password="wrong-password")
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert "ghost-user" not in str(captured)


@pytest.mark.unit
@pytest.mark.integration
def test_configured_username_wrong_password_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = _login(password="wrong-password")
    assert response.status_code == 401
    assert captured["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in str(captured.get("metadata", {}))


@pytest.mark.unit
@pytest.mark.integration
def test_invalid_csrf_failure_audit_actor_is_anonymous() -> None:
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=False):
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                response = _login(csrf_token="wrong-csrf")
    assert response.status_code == 400
    assert captured["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_lockout_transition_audit_actor_is_anonymous(
    rate_limit_store: FakeRateLimitStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_RATE_LIMIT", "2")
    captured: dict[str, Any] = {}

    def _capture(conn: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    assert _login(password="wrong-password").status_code == 401
                    response = _login(password="wrong-password")
    assert response.status_code == 401
    assert captured["reason"] == "rate_limited"
    assert captured["actor_context"].actor == "anonymous"


@pytest.mark.unit
@pytest.mark.integration
def test_successful_login_audit_retains_authenticated_actor() -> None:
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            with patch(
                "app.admin_routes.db.create_admin_session", return_value=42
            ):
                with patch(
                    "app.admin_routes.audit_service.record_login_success"
                ) as success_audit:
                    response = _login()
    assert response.status_code == 303
    assert success_audit.call_args.kwargs["actor_context"].actor == TEST_USERNAME
    assert success_audit.call_args.kwargs["session_id"] == 42


@pytest.mark.unit
@pytest.mark.integration
def test_login_failure_logs_do_not_contain_candidate_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = "attacker-controlled-username"
    caplog.set_level(logging.INFO)
    with mock_db_connection():
        with patch("app.admin_routes._try_claim_login_flow", return_value=True):
            response = _login(username=candidate, password="wrong-password")
    assert response.status_code == 401
    combined = caplog.text.lower()
    assert candidate.lower() not in combined
    assert TEST_LIMITER_SECRET.lower() not in combined
    assert "src:" not in combined
    assert "acct:" not in combined


@pytest.mark.integration
def test_postgres_persists_hmac_limiter_key_and_anonymous_actor() -> None:
    database_url = _require_database_url()
    settings = _settings_with_secrets()
    source_material = "203.0.113.42"
    expected_key = admin_auth.build_source_rate_limit_key(source_material, settings)
    plain_digest = admin_auth.plain_sha256_limiter_digest("src", source_material)
    assert expected_key != plain_digest

    audit_repo = MagicMock()
    audit_repo.append.return_value = {"id": "evt-1"}
    now = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.commit()
        apply_migrations(bootstrap)

    with _pg_conn(database_url) as conn:
        admission = db.try_admit_admin_login(
            conn,
            limiter_keys=(expected_key,),
            now=now,
            rate_limit=5,
            window_seconds=900,
            lockout_seconds=900,
        )
        assert admission.admitted

        with conn.cursor() as cur:
            cur.execute(
                "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                (expected_key,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["limiter_key"] == expected_key
        assert row["limiter_key"] != plain_digest

        actor = audit_service.record_login_failure(
            conn,
            actor_context=ActorContext(actor="anonymous", correlation_id="corr-pg"),
            reason="invalid_credentials",
            repository=audit_repo,
        )
        assert actor is not None
        append_kwargs = audit_repo.append.call_args.kwargs
        assert append_kwargs["actor"] == "anonymous"
        assert TEST_USERNAME not in str(append_kwargs)
