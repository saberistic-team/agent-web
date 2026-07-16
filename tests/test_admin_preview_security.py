"""Security tests for database-isolated, read-only admin preview mode (#331)."""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview_guard import PREVIEW_ALLOWED_METHODS_HEADER
from app.admin_routes import PREVIEW_SESSION_TOKEN
from app.admin_security import AdminPreviewConfigError, validate_admin_preview_config
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

COMPANY_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
BRIEF_ID = "4"
BATCH_ID = "1"

_PREVIEW_ADMIN_USERNAME = "preview-admin"
_PREVIEW_PASSWORD_HASH = PasswordHasher().hash("preview")
_PREVIEW_SESSION_SECRET = "preview-session-secret-32chars-minimum"
_PREVIEW_LIMITER_SECRET = "preview-limiter-secret-32chars-minimum!!"


def _configure_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ADMIN_USERNAME", _PREVIEW_ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _PREVIEW_PASSWORD_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", _PREVIEW_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", _PREVIEW_LIMITER_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)


def _fill_admin_path_params(path: str) -> str:
    filled = path
    filled = filled.replace("{company_id}", COMPANY_ID)
    filled = filled.replace("{contact_id}", CONTACT_ID)
    filled = filled.replace("{brief_id}", BRIEF_ID)
    filled = filled.replace("{batch_id}", BATCH_ID)
    return filled


def _iter_admin_api_routes() -> Iterator[tuple[str, frozenset[str]]]:
    stack: list[Any] = [app.router]
    while stack:
        parent = stack.pop()
        routes = getattr(parent, "routes", None)
        if not routes:
            continue
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                stack.append(route.original_router)
                continue
            if isinstance(route, Mount):
                child = route.app
                if hasattr(child, "routes"):
                    stack.append(child)
                continue
            if isinstance(route, APIRoute):
                path = route.path
                if path.startswith("/admin"):
                    yield path, frozenset(route.methods or set())


def _admin_unsafe_post_routes() -> list[str]:
    unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}
    paths: list[str] = []
    for path, methods in _iter_admin_api_routes():
        if methods & unsafe_methods:
            paths.append(_fill_admin_path_params(path))
    return sorted(set(paths))


@pytest.mark.unit
def test_validate_admin_preview_config_rejects_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
    settings = get_settings()
    with pytest.raises(AdminPreviewConfigError, match="DATABASE_URL"):
        validate_admin_preview_config(settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_name",
    [
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "RESEND_API_KEY",
        "PLAUSIBLE_API_KEY",
    ],
)
def test_validate_admin_preview_config_rejects_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(env_name, "live-provider-secret-value")
    settings = get_settings()
    with pytest.raises(AdminPreviewConfigError, match=env_name):
        validate_admin_preview_config(settings)


@pytest.mark.unit
def test_admin_route_inventory_lists_current_unsafe_methods() -> None:
    routes = _admin_unsafe_post_routes()
    assert "/admin/login" in routes
    assert "/admin/logout" in routes
    assert f"/admin/companies/{COMPANY_ID}/archive" in routes
    assert f"/admin/briefs/{BRIEF_ID}/convert" in routes
    assert f"/admin/pipeline/{COMPANY_ID}/stage" in routes
    assert "/admin/api/imports/linkedin/commit" in routes


@pytest.mark.unit
@pytest.mark.parametrize("path", _admin_unsafe_post_routes())
def test_preview_denies_every_registered_admin_unsafe_route(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _configure_preview(monkeypatch)
    response = client.post(
        path,
        data={"csrf_token": "ignored"},
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code == 405
    assert response.headers.get("allow") == PREVIEW_ALLOWED_METHODS_HEADER


@pytest.mark.unit
def test_preview_denies_unregistered_future_admin_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)
    response = client.post("/admin/future-unregistered-mutation")
    assert response.status_code == 405
    assert response.headers.get("allow") == PREVIEW_ALLOWED_METHODS_HEADER


@pytest.mark.unit
@pytest.mark.parametrize(
    "method",
    ["PUT", "PATCH", "DELETE", "TRACE", "OPTIONS"],
)
def test_preview_denies_other_unsafe_methods(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    _configure_preview(monkeypatch)
    response = client.request(method, "/admin/companies")
    assert response.status_code == 405
    assert response.headers.get("allow") == PREVIEW_ALLOWED_METHODS_HEADER


@pytest.mark.unit
def test_preview_denies_post_without_parsing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)

    def _body_parser_should_not_run(*_args: object, **_kwargs: object) -> MagicMock:
        raise AssertionError("request body parser must not run under preview guard")

    with patch("starlette.requests.Request.body", side_effect=_body_parser_should_not_run):
        with patch("starlette.requests.Request.form", side_effect=_body_parser_should_not_run):
            response = client.post(
                "/admin/login",
                data={"username": "x", "password": "y", "csrf_token": "z"},
            )
    assert response.status_code == 405


@pytest.mark.unit
def test_preview_denies_json_multipart_and_oversized_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)
    json_response = client.post(
        "/admin/api/imports/linkedin/commit",
        json={"rows": [{"name": "Example"}]},
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert json_response.status_code == 405

    files = {"file": ("connections.csv", b"Name,Email\n", "text/csv")}
    multipart_response = client.post(
        "/admin/companies",
        data={"name": "Acme"},
        files=files,
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert multipart_response.status_code == 405

    oversized = client.post(
        "/admin/login",
        content=b"x" * 1_000_000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert oversized.status_code == 405


@pytest.mark.unit
def test_preview_mutations_have_zero_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)

    class _PreviewSideEffectViolation(RuntimeError):
        pass

    def _raise_if_called(*_args: object, **_kwargs: object) -> None:
        raise _PreviewSideEffectViolation("preview must not touch production stores")

    patches = (
        patch("app.db.db_connection", side_effect=_raise_if_called),
        patch("app.db.init_db", side_effect=_raise_if_called),
        patch("app.admin_routes.db.db_connection", side_effect=_raise_if_called),
        patch("app.admin_pipeline_routes.db.db_connection", side_effect=_raise_if_called),
        patch("stripe.checkout.Session.create", side_effect=_raise_if_called),
        patch("app.email_service.notify_team_of_new_brief", side_effect=_raise_if_called),
    )
    flows = (
        ("/admin/login", {"username": "u", "password": "p", "csrf_token": "c"}),
        ("/admin/logout", {"csrf_token": "c"}),
        ("/admin/companies", {"name": "Acme", "csrf_token": "c"}),
        (
            f"/admin/companies/{COMPANY_ID}/edit",
            {"name": "Acme", "csrf_token": "c"},
        ),
        (f"/admin/companies/{COMPANY_ID}/archive", {"csrf_token": "c"}),
        (f"/admin/companies/{COMPANY_ID}/restore", {"csrf_token": "c"}),
        (
            f"/admin/companies/{COMPANY_ID}/research",
            {"summary": "note", "csrf_token": "c"},
        ),
        ("/admin/contacts", {"full_name": "Pat", "csrf_token": "c"}),
        (
            f"/admin/contacts/{CONTACT_ID}/edit",
            {"full_name": "Pat", "csrf_token": "c"},
        ),
        (f"/admin/contacts/{CONTACT_ID}/archive", {"csrf_token": "c"}),
        (f"/admin/contacts/{CONTACT_ID}/restore", {"csrf_token": "c"}),
        (
            f"/admin/contacts/{CONTACT_ID}/research",
            {"summary": "note", "csrf_token": "c"},
        ),
        (
            f"/admin/briefs/{BRIEF_ID}/convert",
            {"company_choice": "new", "contact_choice": "new", "csrf_token": "c"},
        ),
        (f"/admin/pipeline/{COMPANY_ID}/stage", {"stage": "researching", "csrf_token": "c"}),
        (
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            {"next_action": "Follow up", "csrf_token": "c"},
        ),
        (
            f"/admin/pipeline/{COMPANY_ID}/activities",
            {"activity_type": "note", "summary": "Ping", "csrf_token": "c"},
        ),
        (f"/admin/imports/batches/{BATCH_ID}/rollback", {"csrf_token": "c"}),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        for path, data in flows:
            response = client.post(
                path,
                data=data,
                cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
            )
            assert response.status_code == 405, path
        commit = client.post(
            "/admin/api/imports/linkedin/commit",
            json={"batch_id": BATCH_ID},
            cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
        )
        assert commit.status_code == 405


@pytest.mark.unit
def test_preview_get_renders_fixture_pages_and_head_passes_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert "Preview data — not production" in dashboard.text
    assert "Overdue next actions" in dashboard.text

    briefs = client.get("/admin/briefs")
    assert briefs.status_code == 200
    assert "brief-table" in briefs.text

    head = client.head("/admin/briefs")
    assert head.headers.get("allow") != PREVIEW_ALLOWED_METHODS_HEADER


@pytest.mark.unit
def test_preview_disabled_authenticated_post_still_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("correct-horse-battery-staple"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "test-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "test-limiter-secret-32chars-minimum!!")

    crm = MagicMock()
    crm.archive_company.return_value = {"id": UUID(COMPANY_ID)}
    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes.require_admin_session") as require_session,
        patch("app.admin_routes._verify_session_csrf"),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        require_session.return_value = MagicMock(id=1)
        response = client.post(
            f"/admin/companies/{COMPANY_ID}/archive",
            data={"csrf_token": "valid"},
            cookies={SESSION_COOKIE_NAME: "live-session"},
        )
    assert response.status_code == 303
    crm.archive_company.assert_called_once()


@pytest.mark.unit
def test_production_base_url_disables_preview_even_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    settings = get_settings()
    assert settings.admin_preview_enabled is False

    response = client.post("/admin/login", data={"username": "x", "password": "y"})
    assert response.status_code != 405


@pytest.mark.unit
def test_forwarded_host_cannot_enable_preview_on_production_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://staging.saberistic.com")
    settings = get_settings()
    assert settings.admin_preview_enabled is False

    response = client.post(
        "/admin/companies",
        data={"name": "Acme"},
        headers={
            "host": "127.0.0.1:8765",
            "x-forwarded-host": "127.0.0.1:8765",
        },
        cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
    )
    assert response.status_code != 405


@pytest.mark.unit
def test_preview_login_page_still_renders_for_screenshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_preview(monkeypatch)
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "Admin sign in" in response.text
