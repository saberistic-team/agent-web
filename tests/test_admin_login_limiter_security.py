"""Keyed admin login limiter identifiers and anonymous failure audit actors."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row

from app import admin_auth, audit_service, db
from app.admin_security import validate_admin_auth_secrets, validate_admin_login_limiter_secret
from app.actor_context import anonymous_actor_context
from app.config import Settings, get_settings
from app.migrations.runner import apply_migrations

TEST_LIMITER_SECRET_A = "test-login-limiter-secret-32chars-min"
TEST_LIMITER_SECRET_B = "rotated-login-limiter-secret-32chars-mi"


def _plain_sha256_limiter_key(prefix: str, material: str) -> str:
    return hashlib.sha256(f"{prefix}:{material}".encode("utf-8")).hexdigest()


def _settings_with_secrets(
    *,
    current: str,
    previous: str = "",
) -> Settings:
    base = get_settings()
    return Settings(
        database_url=base.database_url,
        stripe_secret_key=base.stripe_secret_key,
        stripe_webhook_secret=base.stripe_webhook_secret,
        stripe_publishable_key=base.stripe_publishable_key,
        resend_api_key=base.resend_api_key,
        from_email=base.from_email,
        notify_email=base.notify_email,
        base_url=base.base_url,
        plausible_domain=base.plausible_domain,
        plausible_api_key=base.plausible_api_key,
        analytics_environment=base.analytics_environment,
        admin_username=base.admin_username,
        admin_password_hash=base.admin_password_hash,
        admin_session_secret=base.admin_session_secret,
        admin_login_limiter_secret=current,
        admin_login_limiter_secret_previous=previous,
        admin_session_ttl_seconds=base.admin_session_ttl_seconds,
        admin_login_rate_limit=base.admin_login_rate_limit,
        admin_login_rate_window_seconds=base.admin_login_rate_window_seconds,
        admin_login_lockout_seconds=base.admin_login_lockout_seconds,
        admin_trust_proxy_headers=base.admin_trust_proxy_headers,
        audit_page_size=base.audit_page_size,
        brief_page_size=base.brief_page_size,
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings_with_secrets(current=TEST_LIMITER_SECRET_A)
    source = "203.0.113.1"
    key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = _plain_sha256_limiter_key("src", source.lower())
    assert key != plain
    assert len(key) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings_with_secrets(current=TEST_LIMITER_SECRET_A)
    settings_b = _settings_with_secrets(current=TEST_LIMITER_SECRET_B)
    source = "203.0.113.1"
    key_a = admin_auth.build_source_rate_limit_key(source, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(source, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_is_stable_for_same_inputs() -> None:
    settings = _settings_with_secrets(current=TEST_LIMITER_SECRET_A)
    first = admin_auth.build_account_rate_limit_key("operator", settings)
    second = admin_auth.build_account_rate_limit_key("operator", settings)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation_for_source_and_account() -> None:
    settings = _settings_with_secrets(current=TEST_LIMITER_SECRET_A)
    shared_material = "operator"
    source_key = admin_auth.build_source_rate_limit_key(shared_material, settings)
    account_key = admin_auth.build_account_rate_limit_key(shared_material, settings)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "field_name"),
    [
        ("", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("short", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("changeme", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ADMIN_LOGIN_LIMITER_SECRET"),
        ("  padded-secret-32chars-minimumxx  ", "ADMIN_LOGIN_LIMITER_SECRET"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(
    secret: str,
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        validate_admin_login_limiter_secret(secret, field_name=field_name)


@pytest.mark.unit
def test_limiter_secret_validation_accepts_strong_material() -> None:
    validate_admin_login_limiter_secret(TEST_LIMITER_SECRET_A)


@pytest.mark.unit
def test_rotation_previous_secret_produces_distinct_identifiers() -> None:
    settings = _settings_with_secrets(
        current=TEST_LIMITER_SECRET_A,
        previous=TEST_LIMITER_SECRET_B,
    )
    current = admin_auth.build_source_rate_limit_key("203.0.113.9", settings)
    previous = admin_auth._digest_limiter_key(
        "src", "203.0.113.9", settings.admin_login_limiter_secret_previous
    )
    assert current != previous


@pytest.mark.unit
def test_rotation_window_honors_previous_key_lockout() -> None:
    settings = _settings_with_secrets(
        current=TEST_LIMITER_SECRET_A,
        previous=TEST_LIMITER_SECRET_B,
    )
    previous_key = admin_auth._digest_limiter_key(
        "src", "testclient", settings.admin_login_limiter_secret_previous
    )
    conn = MagicMock()

    with patch(
        "app.admin_auth.db.is_admin_login_throttled",
        side_effect=lambda _conn, *, limiter_key, now: limiter_key == previous_key,
    ):
        with patch("app.admin_auth.db.db_connection") as db_conn:
            db_conn.return_value.__enter__.return_value = conn
            from starlette.requests import Request

            scope = {
                "type": "http",
                "headers": [],
                "client": ("testclient", 12345),
                "method": "POST",
                "path": "/admin/login",
            }
            request = Request(scope)
            assert admin_auth.is_login_throttled(request, settings, username="")


@pytest.mark.unit
def test_record_login_failure_audit_uses_anonymous_actor() -> None:
    conn = MagicMock()
    repo = MagicMock()
    repo.append.return_value = {"id": "evt-auth"}
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-request-id", b"trace-242")],
        "method": "POST",
        "path": "/admin/login",
    }
    request = Request(scope)
    actor = anonymous_actor_context(request)

    audit_service.record_login_failure(
        conn,
        actor_context=actor,
        reason="invalid_credentials",
        repository=repo,
    )

    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    assert "operator" not in str(append_kwargs)
    assert append_kwargs["summary_after"] == {"reason": "invalid_credentials"}
    assert append_kwargs["metadata"] == {"reason": "invalid_credentials"}


@pytest.mark.unit
def test_login_failure_route_records_anonymous_actor_without_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH", PasswordHasher().hash("correct-horse-battery-staple")
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_A)
    monkeypatch.setenv("BASE_URL", "http://testserver")

    captured: dict[str, Any] = {}

    def _spy_record_login_failure(
        conn: Any,
        *,
        actor_context: Any,
        reason: str,
        repository: Any = None,
    ) -> dict[str, Any]:
        captured["actor"] = actor_context.actor
        captured["reason"] = reason
        captured["repository_kwargs"] = {
            "action": audit_service.ACTION_AUTH_LOGIN_FAILURE,
        }
        return {"id": "evt"}

    client = TestClient(app, follow_redirects=False)
    with (
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._try_claim_login_flow", return_value=True),
        patch(
            "app.admin_routes.audit_service.record_login_failure",
            side_effect=_spy_record_login_failure,
        ),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.post(
            "/admin/login",
            data={
                "username": "attacker-supplied-name",
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )
    assert response.status_code == 401
    assert captured["actor"] == "anonymous"
    assert captured["reason"] == "invalid_credentials"


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(ValueError, match="ADMIN_LOGIN_LIMITER_SECRET"):
        validate_admin_auth_secrets(settings)


@pytest.mark.unit
def test_login_failure_logs_exclude_candidate_and_secrets(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    from app.main import app

    candidate = "candidate-user-242"
    secret = TEST_LIMITER_SECRET_A
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH", PasswordHasher().hash("correct-horse-battery-staple")
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", secret)
    monkeypatch.setenv("BASE_URL", "http://testserver")

    client = TestClient(app, follow_redirects=False)
    caplog.set_level(logging.INFO)
    with (
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._try_claim_login_flow", return_value=True),
        patch("app.admin_routes.audit_service.record_login_failure"),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        client.post(
            "/admin/login",
            data={
                "username": candidate,
                "password": "wrong-password",
                "csrf_token": "flow-csrf",
            },
        )

    combined = caplog.text
    assert candidate not in combined
    assert secret not in combined
    assert "203.0.113" not in combined


_REQUIRED = (pytest.importorskip("os").environ.get("REQUIRE_TEST_DATABASE") or "").strip() in {
    "1",
    "true",
    "yes",
}
_DATABASE_URL = (pytest.importorskip("os").environ.get("TEST_DATABASE_URL") or "").strip()


def _require_database_url() -> str:
    if _DATABASE_URL:
        return _DATABASE_URL
    if _REQUIRED:
        pytest.fail("REQUIRE_TEST_DATABASE=1 but TEST_DATABASE_URL is unset")
    pytest.skip("TEST_DATABASE_URL not set; skipping live Postgres limiter identifier tests")


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers_and_anonymous_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    database_url = _require_database_url()
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET_A)
    settings = _settings_with_secrets(current=TEST_LIMITER_SECRET_A)
    source = "203.0.113.242"
    source_key = admin_auth.build_source_rate_limit_key(source, settings)
    plain = _plain_sha256_limiter_key("src", source.lower())

    with psycopg.connect(database_url, autocommit=False) as bootstrap:
        bootstrap.execute("DROP SCHEMA IF EXISTS public CASCADE")
        bootstrap.execute("CREATE SCHEMA public")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        bootstrap.execute("GRANT ALL ON SCHEMA public TO public")
        apply_migrations(bootstrap)

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=False) as conn:
        try:
            admission = db.try_admit_admin_login(
                conn,
                limiter_keys=(source_key,),
                now=now,
                rate_limit=5,
                window_seconds=900,
                lockout_seconds=900,
            )
            assert admission.admitted
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
                    (source_key,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row["limiter_key"] == source_key
            assert row["limiter_key"] != plain

            repo = MagicMock()
            repo.append.return_value = {"id": "evt"}
            from starlette.requests import Request

            request = Request(
                {
                    "type": "http",
                    "headers": [],
                    "method": "POST",
                    "path": "/admin/login",
                }
            )
            audit_service.record_login_failure(
                conn,
                actor_context=anonymous_actor_context(request),
                reason="invalid_credentials",
                repository=repo,
            )
            assert repo.append.call_args.kwargs["actor"] == "anonymous"
        finally:
            conn.rollback()
            with psycopg.connect(database_url, autocommit=False) as cleanup:
                cleanup.execute("DROP SCHEMA IF EXISTS public CASCADE")
                cleanup.execute("CREATE SCHEMA public")
                cleanup.commit()
