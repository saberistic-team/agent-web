"""Tests for database-isolated, centrally read-only admin preview mode (#331)."""

from __future__ import annotations

import asyncio
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.admin_pipeline_routes import router as admin_pipeline_router
from app.admin_routes import router as admin_router
from app.admin_preview_guard import (
    PREVIEW_ALLOW_HEADER,
    PREVIEW_FORBIDDEN_ENV_VARS,
    PREVIEW_SAFE_UNSAFE_METHOD_PATHS,
    AdminPreviewConfigError,
    validate_admin_preview_config,
)
from app.config import get_settings
from app.main import app, lifespan
from screenshot_deploy import (
    PREVIEW_ADMIN_LOGIN_LIMITER_SECRET,
    PREVIEW_ADMIN_PASSWORD_HASH,
    PREVIEW_ADMIN_SESSION_SECRET,
    PREVIEW_ADMIN_USERNAME,
    build_preview_server_env,
)

PREVIEW_BASE = "http://127.0.0.1:8765"
COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
BRIEF_ID = "42"
BATCH_ID = "11111111-1111-1111-1111-111111111111"

UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "TRACE")


def _preview_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", PREVIEW_BASE)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", PREVIEW_ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PREVIEW_ADMIN_PASSWORD_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", PREVIEW_ADMIN_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", PREVIEW_ADMIN_LOGIN_LIMITER_SECRET)


def _sample_admin_path(route_path: str) -> str:
    return (
        route_path.replace("{company_id}", COMPANY_ID)
        .replace("{contact_id}", CONTACT_ID)
        .replace("{brief_id}", BRIEF_ID)
        .replace("{batch_id}", BATCH_ID)
        .replace("{full_path:path}", "companies")
    )


def _iter_admin_api_routes() -> Iterator[tuple[str, frozenset[str]]]:
    for router in (admin_router, admin_pipeline_router):
        for route in router.routes:
            if isinstance(route, APIRoute):
                yield route.path, frozenset(route.methods or set())


def _admin_unsafe_routes() -> list[tuple[str, frozenset[str]]]:
    unsafe: list[tuple[str, frozenset[str]]] = []
    for path, methods in _iter_admin_api_routes():
        blocked = methods - {"GET", "HEAD", "OPTIONS"}
        if blocked:
            unsafe.append((path, blocked))
    return unsafe


@pytest.mark.unit
def test_build_preview_server_env_clears_parent_secrets() -> None:
    parent = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql://prod:secret@db.example/prod",
        "STRIPE_SECRET_KEY": "sk_live_sentinel",
        "STRIPE_WEBHOOK_SECRET": "whsec_sentinel",
        "RESEND_API_KEY": "re_sentinel",
        "PLAUSIBLE_API_KEY": "plausible_sentinel",
        "ADMIN_USERNAME": "parent-admin",
        "ADMIN_PASSWORD_HASH": "parent-hash",
    }
    env = build_preview_server_env(PREVIEW_BASE, parent_environ=parent)

    assert env["PATH"] == "/usr/bin"
    assert env["BASE_URL"] == PREVIEW_BASE
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["DATABASE_URL"] == ""
    assert env["STRIPE_SECRET_KEY"] == ""
    assert env["STRIPE_WEBHOOK_SECRET"] == ""
    assert env["RESEND_API_KEY"] == ""
    assert env["PLAUSIBLE_API_KEY"] == ""
    assert env["ADMIN_USERNAME"] == PREVIEW_ADMIN_USERNAME
    assert env["ADMIN_PASSWORD_HASH"] == PREVIEW_ADMIN_PASSWORD_HASH
    assert "parent-admin" not in env.values()
    assert "sk_live_sentinel" not in env.values()
    assert "postgresql://prod" not in env.values()


@pytest.mark.unit
def test_build_preview_server_env_does_not_inherit_full_parent_environ() -> None:
    parent = {
        "PATH": "/bin",
        "GITHUB_TOKEN": "ghp_sentinel",
        "OPENAI_API_KEY": "sk-sentinel",
        "CURSOR_API_KEY": "cursor-sentinel",
    }
    env = build_preview_server_env(PREVIEW_BASE, parent_environ=parent)
    assert env.get("GITHUB_TOKEN") is None
    assert env.get("OPENAI_API_KEY") is None
    assert env.get("CURSOR_API_KEY") is None


@pytest.mark.unit
def test_validate_admin_preview_config_rejects_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    settings = get_settings()
    with pytest.raises(AdminPreviewConfigError, match="DATABASE_URL"):
        validate_admin_preview_config(settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_name", [name for name, _label in PREVIEW_FORBIDDEN_ENV_VARS]
)
def test_validate_admin_preview_config_rejects_forbidden_env(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv(env_name, "sentinel-value")
    settings = get_settings()
    with pytest.raises(AdminPreviewConfigError, match=env_name):
        validate_admin_preview_config(settings)


@pytest.mark.unit
def test_lifespan_fails_when_preview_and_database_url_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")

    async def _run() -> None:
        async with lifespan(app):
            pass

    with pytest.raises(AdminPreviewConfigError):
        asyncio.run(_run())


@pytest.mark.unit
def test_lifespan_preview_skips_database_init(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)

    async def _run() -> None:
        with patch("app.main.db.init_db") as init_db:
            async with lifespan(app):
                pass
            init_db.assert_not_called()

    asyncio.run(_run())


@pytest.mark.unit
def test_admin_route_inventory_has_unsafe_methods() -> None:
    routes = _admin_unsafe_routes()
    paths = {path for path, _ in routes}
    assert "/admin/login" in paths
    assert "/admin/companies" in paths
    assert "/admin/api/imports/linkedin/commit" in paths
    assert any("/admin/pipeline/" in path for path in paths)


@pytest.mark.unit
@pytest.mark.parametrize("method", UNSAFE_METHODS)
def test_preview_denies_unsafe_methods_on_admin_routes(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    for path, methods in _admin_unsafe_routes():
        if method not in methods:
            continue
        sample = _sample_admin_path(path)
        if sample in PREVIEW_SAFE_UNSAFE_METHOD_PATHS:
            # Explicitly exempted, read-only-by-design preview computation —
            # covered by test_preview_allows_safe_unsafe_method_exemptions.
            continue
        response = client.request(method, sample)
        assert response.status_code == 405, f"{method} {sample} -> {response.status_code}"
        assert response.headers.get("allow") == PREVIEW_ALLOW_HEADER


@pytest.mark.unit
def test_preview_allows_safe_unsafe_method_exemptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly exempted read-only preview computations still return 200,
    never a 405, even though they use an unsafe HTTP method."""
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    assert PREVIEW_SAFE_UNSAFE_METHOD_PATHS
    for path in PREVIEW_SAFE_UNSAFE_METHOD_PATHS:
        response = client.post(path, json={"connections": []})
        assert response.status_code != 405, f"POST {path} -> {response.status_code}"


@pytest.mark.unit
def test_preview_denies_post_before_body_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)

    db_called = False

    def _fail_db(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal db_called
        db_called = True
        raise AssertionError("database must not be opened in preview POST")

    payloads = [
        ("application/json", b'{"connections":[{"profile_url":"https://x"}]}'),
        ("application/x-www-form-urlencoded", b"name=Acme&domain=acme.test"),
        (
            "multipart/form-data; boundary=----preview",
            b"------preview\r\nContent-Disposition: form-data; name=\"file\"; "
            b'filename="x.csv"\r\n\r\na,b\r\n------preview--\r\n',
        ),
        ("text/plain", b"x" * 1_000_000),
        ("application/json", b"{not-json"),
    ]
    with patch("app.db.db_connection", side_effect=_fail_db):
        for content_type, body in payloads:
            response = client.post(
                "/admin/api/imports/linkedin/commit",
                content=body,
                headers={"content-type": content_type},
            )
            assert response.status_code == 405
            assert response.headers.get("allow") == PREVIEW_ALLOW_HEADER
    assert not db_called


@pytest.mark.unit
def test_preview_mutation_routes_have_zero_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)

    db_called = False
    stripe_called = False
    email_called = False

    def _fail_db(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal db_called
        db_called = True
        raise AssertionError("database must not be opened")

    def _fail_stripe(*args: Any, **kwargs: Any) -> None:
        nonlocal stripe_called
        stripe_called = True
        raise AssertionError("stripe must not be called")

    def _fail_email(*args: Any, **kwargs: Any) -> None:
        nonlocal email_called
        email_called = True
        raise AssertionError("email must not be called")

    targets = [
        ("/admin/login", {"username": "x", "password": "y", "csrf_token": "z"}),
        ("/admin/logout", {"csrf_token": "z"}),
        ("/admin/companies", {"name": "Acme", "csrf_token": "z"}),
        (f"/admin/companies/{COMPANY_ID}/edit", {"name": "Acme", "csrf_token": "z"}),
        (f"/admin/companies/{COMPANY_ID}/archive", {"csrf_token": "z"}),
        (f"/admin/companies/{COMPANY_ID}/restore", {"csrf_token": "z"}),
        (f"/admin/companies/{COMPANY_ID}/research", {"csrf_token": "z"}),
        ("/admin/contacts", {"full_name": "Ada", "csrf_token": "z"}),
        (f"/admin/contacts/{CONTACT_ID}/edit", {"full_name": "Ada", "csrf_token": "z"}),
        (f"/admin/contacts/{CONTACT_ID}/archive", {"csrf_token": "z"}),
        (f"/admin/contacts/{CONTACT_ID}/restore", {"csrf_token": "z"}),
        (f"/admin/contacts/{CONTACT_ID}/research", {"csrf_token": "z"}),
        (f"/admin/briefs/{BRIEF_ID}/convert", {"csrf_token": "z", "company_choice": "new"}),
        (f"/admin/pipeline/{COMPANY_ID}/stage", {"csrf_token": "z", "to_stage": "won"}),
        (f"/admin/pipeline/{COMPANY_ID}/next-action", {"csrf_token": "z", "next_action": "x"}),
        (f"/admin/pipeline/{COMPANY_ID}/activities", {"csrf_token": "z", "summary": "x"}),
        (f"/admin/imports/batches/{BATCH_ID}/rollback", {"csrf_token": "z"}),
    ]

    with (
        patch("app.db.db_connection", side_effect=_fail_db),
        patch("app.db.init_db", side_effect=_fail_db),
        patch("app.stripe_service.create_checkout_session", side_effect=_fail_stripe),
        patch("app.email_service.send_email", side_effect=_fail_email),
    ):
        for path, data in targets:
            response = client.post(path, data=data)
            assert response.status_code == 405, path
        json_response = client.post(
            "/admin/api/imports/linkedin/commit",
            json={"connections": [{"profile_url": "https://linkedin.com/in/ada"}]},
        )
        assert json_response.status_code == 405

    assert not db_called
    assert not stripe_called
    assert not email_called


@pytest.mark.unit
def test_preview_get_renders_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    client = TestClient(app, follow_redirects=False)

    response = client.get("/admin")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Overdue next actions" in response.text


@pytest.mark.unit
def test_preview_head_not_blocked_by_read_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD is permitted through the guard; individual routes may still lack HEAD handlers."""
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    response = client.head("/admin")
    assert response.headers.get("allow") != PREVIEW_ALLOW_HEADER


@pytest.mark.unit
def test_preview_disabled_authenticated_post_still_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta, timezone

    from app.admin_auth import AdminSession

    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!!")

    assert not get_settings().admin_preview_enabled
    fake_session = AdminSession(
        id=1,
        admin_username="operator",
        token_hash="hash",
        csrf_token_hash="csrf-hash",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    with (
        patch("app.admin_routes.require_admin_session", return_value=fake_session),
        patch("app.admin_routes._verify_session_csrf"),
        patch("app.admin_routes._crm") as crm,
        patch("app.admin_routes.db.db_connection") as db_conn,
    ):
        crm.rollback_import_batch.return_value = {"batch": {"id": BATCH_ID}}
        db_conn.return_value.__enter__.return_value = MagicMock()
        client = TestClient(app, follow_redirects=False)
        response = client.post(
            f"/admin/imports/batches/{BATCH_ID}/rollback",
            data={"csrf_token": "ok"},
        )
    assert response.status_code == 303
    crm.rollback_import_batch.assert_called_once()


@pytest.mark.unit
def test_production_base_url_disables_preview_despite_mode_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    settings = get_settings()
    assert settings.admin_preview_mode is True
    assert settings.admin_preview_enabled is False


@pytest.mark.unit
def test_forwarded_host_cannot_enable_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    monkeypatch.setenv("ADMIN_USERNAME", PREVIEW_ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PREVIEW_ADMIN_PASSWORD_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", PREVIEW_ADMIN_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", PREVIEW_ADMIN_LOGIN_LIMITER_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/admin",
        headers={
            "host": "saberistic.com",
            "x-forwarded-host": "127.0.0.1",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
def test_preview_post_returns_truthful_allow_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    client = TestClient(app, follow_redirects=False)
    response = client.post("/admin/logout", data={"csrf_token": "x"})
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"
    assert "location" not in response.headers
