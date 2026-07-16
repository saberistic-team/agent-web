"""Security invariants for database-isolated ADMIN_PREVIEW_MODE."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app, lifespan
from app.preview_config import PreviewConfigError, validate_preview_config
from screenshot_deploy import build_preview_server_env

client = TestClient(app, follow_redirects=False)

_PREVIEW_BASE = "http://127.0.0.1:8765"
_PREVIEW_ADMIN_CREDS = {
    "ADMIN_USERNAME": "preview-admin",
    "ADMIN_PASSWORD_HASH": (
        "$argon2id$v=19$m=65536,t=3,p=4$preview-screenshot-salt$preview-screenshot-hash"
    ),
    "ADMIN_SESSION_SECRET": "preview-session-secret-32chars-minimum",
    "ADMIN_LOGIN_LIMITER_SECRET": "preview-limiter-secret-32chars-minimum",
}

_PARAM_SAMPLES: dict[str, str] = {
    "company_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "contact_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "brief_id": "1",
    "batch_id": "11111111-1111-1111-1111-111111111111",
}

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE", "TRACE"})


@pytest.fixture
def preview_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", _PREVIEW_BASE)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("STRIPE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    for key, value in _PREVIEW_ADMIN_CREDS.items():
        monkeypatch.setenv(key, value)


def _fill_admin_path(path: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return _PARAM_SAMPLES.get(name, "00000000-0000-0000-0000-000000000001")

    return re.sub(r"\{(\w+)\}", repl, path)


def iter_admin_routes() -> Iterator[tuple[str, str]]:
    def _walk(routes) -> Iterator[APIRoute]:  # noqa: ANN001
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                yield from _walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                yield from _walk(route.routes)
            elif isinstance(route, APIRoute):
                yield route

    for route in _walk(app.routes):
        if not route.path.startswith("/admin"):
            continue
        for method in sorted(route.methods):
            yield route.path, method


def _assert_method_not_allowed(response) -> None:
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"


@pytest.mark.unit
def test_build_preview_server_env_strips_parent_secrets() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/ci",
        "DATABASE_URL": "postgresql://prod:secret@db.example/app",
        "STRIPE_SECRET_KEY": "sk_live_parent",
        "STRIPE_WEBHOOK_SECRET": "whsec_parent",
        "RESEND_API_KEY": "re_parent",
        "PLAUSIBLE_API_KEY": "plausible_parent",
        "GITHUB_TOKEN": "ghp_parent",
    }
    env = build_preview_server_env(
        base_url="http://127.0.0.1:8765",
        parent_environ=parent,
    )
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/ci"
    assert env["DATABASE_URL"] == ""
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["BASE_URL"] == "http://127.0.0.1:8765"
    for blocked in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "RESEND_API_KEY",
        "PLAUSIBLE_API_KEY",
        "GITHUB_TOKEN",
    ):
        assert blocked not in env


@pytest.mark.unit
def test_validate_preview_config_rejects_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    with pytest.raises(PreviewConfigError, match="DATABASE_URL"):
        validate_preview_config(get_settings())


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_name",
    [
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PUBLISHABLE_KEY",
        "RESEND_API_KEY",
        "PLAUSIBLE_API_KEY",
    ],
)
def test_validate_preview_config_rejects_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(env_name, "secret-value")
    with pytest.raises(PreviewConfigError, match=env_name):
        validate_preview_config(get_settings())


@pytest.mark.unit
def test_lifespan_rejects_preview_with_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", _PREVIEW_BASE)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")

    async def _run() -> None:
        async with lifespan(app):
            pass

    with pytest.raises(PreviewConfigError):
        asyncio.run(_run())


@pytest.mark.unit
def test_lifespan_skips_db_init_in_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", _PREVIEW_BASE)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key, value in _PREVIEW_ADMIN_CREDS.items():
        monkeypatch.setenv(key, value)

    async def _run() -> None:
        with patch("app.main.db.init_db") as init_db:
            async with lifespan(app):
                pass
            init_db.assert_not_called()

    asyncio.run(_run())


@pytest.mark.unit
def test_preview_denies_every_registered_unsafe_admin_method(
    preview_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("psycopg.connect", MagicMock(side_effect=AssertionError("database connect")))
    monkeypatch.setattr(
        "app.db.db_connection",
        MagicMock(side_effect=AssertionError("db_connection")),
    )

    seen: list[tuple[str, str]] = []
    for path, method in iter_admin_routes():
        if method not in _UNSAFE_METHODS:
            continue
        url = _fill_admin_path(path)
        seen.append((method, url))
        response = client.request(method, url, content=b"ignored-body")
        _assert_method_not_allowed(response)

    assert seen, "expected at least one unsafe /admin route in inventory"


@pytest.mark.unit
@pytest.mark.parametrize(
    "content_type,body",
    [
        ("application/json", b'{"csrf_token":"x","company_choice":"new"}'),
        ("application/x-www-form-urlencoded", b"csrf_token=x&name=Acme"),
        (
            "multipart/form-data; boundary=----preview",
            b"------preview\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\nx\r\n------preview--\r\n",
        ),
        ("text/plain", b"x" * 1_000_000),
        ("application/json", b"{not-json"),
    ],
)
def test_preview_unsafe_admin_post_denies_without_parsing_body(
    preview_env: None,
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    body: bytes,
) -> None:
    body_called = False

    async def _forbidden_body(self):  # noqa: ANN001
        nonlocal body_called
        body_called = True
        raise AssertionError("request body must not be read in preview guard")

    monkeypatch.setattr("starlette.requests.Request.body", _forbidden_body, raising=False)

    response = client.post(
        "/admin/companies",
        content=body,
        headers={"content-type": content_type},
    )
    _assert_method_not_allowed(response)
    assert not body_called


@pytest.mark.unit
def test_preview_mutation_routes_have_no_side_effects(
    preview_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("psycopg.connect", MagicMock(side_effect=AssertionError("psycopg.connect")))
    monkeypatch.setattr("httpx.post", MagicMock(side_effect=AssertionError("httpx.post")))
    monkeypatch.setattr(
        "app.email_service.send_email",
        MagicMock(side_effect=AssertionError("send_email")),
    )

    mutation_targets: list[tuple[str, str, dict[str, Any] | None, bytes | None]] = [
        ("POST", "/admin/login", {"username": "x", "password": "y"}, None),
        ("POST", "/admin/logout", {"csrf_token": "x"}, None),
        ("POST", "/admin/companies", {"name": "Acme", "csrf_token": "x"}, None),
        (
            "POST",
            f"/admin/companies/{_PARAM_SAMPLES['company_id']}/edit",
            {"name": "Acme", "csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/companies/{_PARAM_SAMPLES['company_id']}/archive",
            {"csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/companies/{_PARAM_SAMPLES['company_id']}/restore",
            {"csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/companies/{_PARAM_SAMPLES['company_id']}/research",
            {"csrf_token": "x", "record_type": "note", "body": "x"},
            None,
        ),
        ("POST", "/admin/contacts", {"full_name": "Ada", "csrf_token": "x"}, None),
        (
            "POST",
            f"/admin/contacts/{_PARAM_SAMPLES['contact_id']}/edit",
            {"full_name": "Ada", "csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/contacts/{_PARAM_SAMPLES['contact_id']}/archive",
            {"csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/contacts/{_PARAM_SAMPLES['contact_id']}/restore",
            {"csrf_token": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/contacts/{_PARAM_SAMPLES['contact_id']}/research",
            {"csrf_token": "x", "record_type": "note", "body": "x"},
            None,
        ),
        (
            "POST",
            f"/admin/briefs/{_PARAM_SAMPLES['brief_id']}/convert",
            {"csrf_token": "x", "company_choice": "new", "contact_choice": "new"},
            None,
        ),
        (
            "POST",
            f"/admin/pipeline/{_PARAM_SAMPLES['company_id']}/stage",
            {"csrf_token": "x", "to_stage": "qualified"},
            None,
        ),
        (
            "POST",
            f"/admin/pipeline/{_PARAM_SAMPLES['company_id']}/next-action",
            {"csrf_token": "x", "next_action": "Follow up"},
            None,
        ),
        (
            "POST",
            f"/admin/pipeline/{_PARAM_SAMPLES['company_id']}/activities",
            {"csrf_token": "x", "activity_type": "note", "summary": "Ping"},
            None,
        ),
        (
            "POST",
            f"/admin/imports/batches/{_PARAM_SAMPLES['batch_id']}/rollback",
            {"csrf_token": "x"},
            None,
        ),
        (
            "POST",
            "/admin/api/imports/linkedin/commit",
            None,
            b'{"rows":[]}',
        ),
    ]

    for method, url, form_data, raw_body in mutation_targets:
        if raw_body is not None:
            response = client.request(
                method,
                url,
                content=raw_body,
                headers={"content-type": "application/json"},
            )
        else:
            response = client.request(method, url, data=form_data)
        _assert_method_not_allowed(response)


@pytest.mark.unit
def test_preview_get_and_head_admin_pages_render_fixtures(preview_env: None) -> None:
    dash = client.get("/admin")
    assert dash.status_code == 200
    assert "Preview data — not production" in dash.text

    head = client.head("/admin")
    assert head.status_code in {200, 405}


@pytest.mark.unit
def test_preview_disabled_authenticated_post_still_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from app import admin_auth

    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!!")

    session = admin_auth.AdminSession(
        id=1,
        admin_username="operator",
        token_hash="hash",
        csrf_token_hash="csrf-hash",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    crm = MagicMock()
    crm.create_company.return_value = {
        "company": {"id": _PARAM_SAMPLES["company_id"]},
        "duplicate_warnings": [],
    }
    with patch("app.admin_routes.require_admin_session", return_value=session):
        with patch("app.admin_routes._verify_session_csrf"):
            with patch("app.admin_routes._crm", crm):
                with patch("app.admin_routes.db.db_connection") as conn_ctx:
                    conn_ctx.return_value.__enter__.return_value = MagicMock()
                    conn_ctx.return_value.__exit__.return_value = None
                    response = client.post(
                        "/admin/companies",
                        data={"name": "Acme Labs", "csrf_token": "token"},
                    )
    assert response.status_code == 303
    crm.create_company.assert_called_once()


@pytest.mark.unit
def test_production_base_url_disables_preview_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    monkeypatch.setenv("ADMIN_USERNAME", _PREVIEW_ADMIN_CREDS["ADMIN_USERNAME"])
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _PREVIEW_ADMIN_CREDS["ADMIN_PASSWORD_HASH"])
    monkeypatch.setenv("ADMIN_SESSION_SECRET", _PREVIEW_ADMIN_CREDS["ADMIN_SESSION_SECRET"])
    monkeypatch.setenv(
        "ADMIN_LOGIN_LIMITER_SECRET",
        _PREVIEW_ADMIN_CREDS["ADMIN_LOGIN_LIMITER_SECRET"],
    )
    settings = get_settings()
    assert settings.admin_preview_enabled is False

    unauth = client.post("/admin/logout", data={"csrf_token": "x"})
    assert unauth.status_code == 303
    assert unauth.headers["location"].startswith("/admin/login")


@pytest.mark.unit
def test_preview_mode_flag_alone_does_not_enable_guard_on_production_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://staging.saberistic.com")
    assert get_settings().admin_preview_enabled is False
