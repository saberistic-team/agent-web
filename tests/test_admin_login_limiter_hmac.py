"""Tests for HMAC admin login limiter identifiers and anonymous failure actors."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from app import admin_auth, audit_service, db
from app.actor_context import ActorContext
from app.admin_security import (
    AdminSecurityConfigError,
    validate_admin_login_limiter_secret,
    validate_admin_security_secrets,
)
from app.config import Settings, get_settings
from tests.conftest import TEST_LIMITER_SECRET
from tests.test_admin_auth import (
    TEST_SECRET,
    TEST_USERNAME,
    client,
    mock_db_connection,
    shared_rate_limiter,
)

pytest_plugins = [
    "tests.test_admin_auth",
    "tests.test_admin_login_rate_limit_integration",
]

TEST_LIMITER_SECRET_B = "other-limiter-secret-32chars-minimum-y"
TEST_LIMITER_SECRET_PREVIOUS = "prev-limiter-secret-32chars-minimum-z"


def _settings(
    *,
    limiter_secret: str = TEST_LIMITER_SECRET,
    limiter_previous: str = "",
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
        admin_password_hash="hash",
        admin_session_secret=TEST_SECRET,
        admin_login_limiter_secret=limiter_secret,
        admin_login_limiter_secret_previous=limiter_previous,
    )


@pytest.mark.unit
def test_limiter_identifier_is_not_plain_sha256() -> None:
    settings = _settings()
    material = "203.0.113.1"
    plain = hashlib.sha256(f"src:{material}".encode("utf-8")).hexdigest()
    keyed = admin_auth.build_source_rate_limit_key(material, settings)
    assert keyed != plain
    assert len(keyed) == 64


@pytest.mark.unit
def test_limiter_identifier_depends_on_secret() -> None:
    settings_a = _settings(limiter_secret=TEST_LIMITER_SECRET)
    settings_b = _settings(limiter_secret=TEST_LIMITER_SECRET_B)
    material = "203.0.113.1"
    key_a = admin_auth.build_source_rate_limit_key(material, settings_a)
    key_b = admin_auth.build_source_rate_limit_key(material, settings_b)
    assert key_a != key_b


@pytest.mark.unit
def test_limiter_identifier_stable_for_same_input_secret_and_domain() -> None:
    settings = _settings()
    first = admin_auth.build_source_rate_limit_key("203.0.113.1", settings)
    second = admin_auth.digest_limiter_key("src", "203.0.113.1", TEST_LIMITER_SECRET)
    assert first == second


@pytest.mark.unit
def test_limiter_domain_separation() -> None:
    shared_payload = "operator"
    source_key = admin_auth.digest_limiter_key("src", shared_payload, TEST_LIMITER_SECRET)
    account_key = admin_auth.digest_limiter_key("acct", shared_payload, TEST_LIMITER_SECRET)
    assert source_key != account_key


@pytest.mark.unit
@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("", "required"),
        ("short", "at least"),
        ("changeme-please-set-a-real-secret-value", "placeholder"),
    ],
)
def test_limiter_secret_validation_rejects_weak_material(secret: str, message: str) -> None:
    with pytest.raises(AdminSecurityConfigError, match=message):
        validate_admin_login_limiter_secret(secret)


@pytest.mark.unit
def test_startup_validation_requires_limiter_secret_when_admin_auth_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.delenv("ADMIN_LOGIN_LIMITER_SECRET", raising=False)
    settings = get_settings()
    with pytest.raises(AdminSecurityConfigError, match="required"):
        validate_admin_security_secrets(settings)


@pytest.mark.unit
def test_rotation_includes_previous_secret_keys() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET_PREVIOUS,
    )
    keys = admin_auth.login_limiter_keys(
        settings=settings,
        submitted_username=TEST_USERNAME,
        client_source="203.0.113.1",
        configured_admin_username=TEST_USERNAME,
    )
    assert len(keys) == 4
    current_source = admin_auth.digest_limiter_key("src", "203.0.113.1", TEST_LIMITER_SECRET)
    previous_source = admin_auth.digest_limiter_key(
        "src", "203.0.113.1", TEST_LIMITER_SECRET_PREVIOUS
    )
    assert current_source in keys
    assert previous_source in keys


@pytest.mark.unit
def test_rotation_rejects_identical_current_and_previous_secrets() -> None:
    settings = _settings(
        limiter_secret=TEST_LIMITER_SECRET,
        limiter_previous=TEST_LIMITER_SECRET,
    )
    with pytest.raises(AdminSecurityConfigError, match="must differ"):
        validate_admin_security_secrets(settings)


@pytest.mark.integration
def test_previous_key_rows_remain_eligible_for_cleanup(pg_conn: psycopg.Connection) -> None:
    previous_key = admin_auth.digest_limiter_key("src", "203.0.113.88", TEST_LIMITER_SECRET_PREVIOUS)
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(previous_key,),
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


@pytest.mark.unit
def test_unknown_username_failure_audit_uses_anonymous_actor(rate_limit_store) -> None:
    from tests.test_admin_auth import _fetch_login_form, _login

    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt-1"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    _fetch_login_form()
                    response = _login(username="attacker-candidate", password="wrong")
                    assert response.status_code == 401

    assert captured["actor_context"].actor == "anonymous"
    assert "attacker-candidate" not in json.dumps(captured, default=str)


@pytest.mark.unit
def test_configured_username_wrong_password_keeps_anonymous_actor(rate_limit_store) -> None:
    from tests.test_admin_auth import _fetch_login_form, _login

    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt-1"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    side_effect=_capture,
                ):
                    _fetch_login_form()
                    response = _login(username=TEST_USERNAME, password="wrong-password")
                    assert response.status_code == 401

    assert captured["actor_context"].actor == "anonymous"
    assert TEST_USERNAME not in json.dumps(captured, default=str)


@pytest.mark.unit
def test_invalid_csrf_failure_audit_uses_anonymous_actor(rate_limit_store) -> None:
    from tests.test_admin_auth import _fetch_login_form

    captured: dict[str, Any] = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt-1"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch(
                "app.admin_routes.audit_service.record_login_failure",
                side_effect=_capture,
            ):
                csrf_token, cookies = _fetch_login_form()
                response = client.post(
                    "/admin/login",
                    data={
                        "username": "csrf-attacker",
                        "password": "wrong",
                        "csrf_token": "not-the-form-token",
                    },
                    cookies=cookies,
                )
                assert response.status_code == 400

    assert captured["actor_context"].actor == "anonymous"
    assert captured["reason"] == "invalid_csrf"


@pytest.mark.unit
def test_successful_login_retains_administrator_actor(rate_limit_store) -> None:
    from tests.test_admin_auth import _login

    captured: dict[str, Any] = {}

    def _capture_success(conn, **kwargs):
        captured.update(kwargs)
        return {"id": "evt-success"}

    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch("app.admin_routes.db.create_admin_session", return_value=42):
                    with patch(
                        "app.admin_routes.audit_service.record_login_success",
                        side_effect=_capture_success,
                    ):
                        response = _login()
                        assert response.status_code == 303

    assert captured["actor_context"].actor == TEST_USERNAME
    assert captured["session_id"] is not None


@pytest.mark.unit
def test_login_failure_logs_do_not_leak_candidates_or_secrets(
    rate_limit_store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.test_admin_auth import _fetch_login_form, _login

    caplog.set_level(logging.INFO)
    with shared_rate_limiter(rate_limit_store):
        with mock_db_connection():
            with patch("app.admin_routes._try_claim_login_flow", return_value=True):
                with patch(
                    "app.admin_routes.audit_service.record_login_failure",
                    return_value={"id": "evt"},
                ):
                    _fetch_login_form()
                    _login(username="leak-me@example.com", password="secret-password")

    combined = caplog.text + " ".join(str(record.__dict__) for record in caplog.records)
    assert "leak-me@example.com" not in combined
    assert TEST_LIMITER_SECRET not in combined
    assert "src:203.0.113" not in combined.lower()


@pytest.mark.integration
def test_postgres_persists_keyed_limiter_identifiers_and_anonymous_actor(
    pg_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    settings = get_settings()
    source_key = admin_auth.build_source_rate_limit_key("203.0.113.99", settings)
    plain = hashlib.sha256(b"src:203.0.113.99").hexdigest()
    assert source_key != plain

    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    db.try_admit_admin_login(
        pg_conn,
        limiter_keys=(source_key,),
        now=now,
        rate_limit=5,
        window_seconds=900,
        lockout_seconds=900,
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT limiter_key FROM admin_login_rate_limits WHERE limiter_key = %s",
            (source_key,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["limiter_key"] == source_key
    assert len(row["limiter_key"]) == 64

    repo = MagicMock()
    repo.append.return_value = {"id": "evt-db"}
    audit_service.record_login_failure(
        pg_conn,
        actor_context=ActorContext(actor="anonymous", correlation_id="corr-1"),
        reason="invalid_credentials",
        repository=repo,
    )
    append_kwargs = repo.append.call_args.kwargs
    assert append_kwargs["actor"] == "anonymous"
    event_blob = json.dumps(append_kwargs, default=str)
    assert TEST_USERNAME not in event_blob
