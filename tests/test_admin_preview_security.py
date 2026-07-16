"""Security tests for database-isolated, read-only ADMIN_PREVIEW_MODE (#331)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview_guard import (
    PREVIEW_ALLOWED_METHODS,
    AdminPreviewConfigError,
    validate_admin_preview_config,
)
from app.admin_routes import PREVIEW_SESSION_TOKEN
from app.config import get_settings
from app.main import app
from screenshot_deploy import (
    PREVIEW_CLEARED_SECRETS,
    build_preview_child_env,
)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"

client = TestClient(app, follow_redirects=False)

_PATH_PARAM_SAMPLES = {
    "company_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "contact_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "brief_id": "4",
    "batch_id": "11111111-1111-1111-1111-111111111111",
}

_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "TRACE")


def _preview_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in PREVIEW_CLEARED_SECRETS:
        monkeypatch.delenv(key, raising=False)


def _sample_admin_path(path: str) -> str:
    for name, value in _PATH_PARAM_SAMPLES.items():
        path = path.replace(f"{{{name}}}", value)
    return path


def _admin_unsafe_route_methods(application: FastAPI) -> list[tuple[str, frozenset[str]]]:
    inventory: list[tuple[str, frozenset[str]]] = []
    for path, operations in application.openapi()["paths"].items():
        if not path.startswith("/admin"):
            continue
        unsafe = frozenset(
            method.upper()
            for method in operations
            if method.upper() not in PREVIEW_ALLOWED_METHODS
        )
        if unsafe:
            inventory.append((path, unsafe))
    return inventory


@pytest.mark.unit
def test_build_preview_child_env_clears_parent_secrets() -> None:
    parent = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql://prod:secret@db.example/app",
        "STRIPE_SECRET_KEY": "sk_live_prod",
        "STRIPE_WEBHOOK_SECRET": "whsec_prod",
        "RESEND_API_KEY": "re_prod",
        "PLAUSIBLE_API_KEY": "plausible_prod",
        "PLAUSIBLE_DOMAIN": "saberistic.com",
        "ADMIN_PREVIEW_SEED": "99",
    }
    env = build_preview_child_env(port=9999, parent_environ=parent)
    assert env["BASE_URL"] == "http://127.0.0.1:9999"
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert env["ADMIN_PREVIEW_SEED"] == "99"
    for key in PREVIEW_CLEARED_SECRETS:
        assert env.get(key) == ""
    assert "sk_live_prod" not in env.values()
    assert "postgresql://prod" not in env.values()


@pytest.mark.unit
def test_build_preview_child_env_does_not_inherit_unlisted_parent_vars() -> None:
    parent = {
        "PATH": "/bin",
        "GITHUB_TOKEN": "ghp_parent_secret",
        "OPENAI_API_KEY": "sk-parent",
        "CURSOR_API_KEY": "cursor-parent",
    }
    env = build_preview_child_env(parent_environ=parent)
    assert env["PATH"] == "/bin"
    assert "GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CURSOR_API_KEY" not in env


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_name,env_value",
    [
        ("DATABASE_URL", "postgresql://prod/db"),
        ("STRIPE_SECRET_KEY", "sk_live_x"),
        ("STRIPE_WEBHOOK_SECRET", "whsec_x"),
        ("RESEND_API_KEY", "re_x"),
        ("PLAUSIBLE_API_KEY", "plausible_x"),
    ],
)
def test_validate_admin_preview_config_rejects_production_credentials(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv(env_name, env_value)
    with pytest.raises(AdminPreviewConfigError, match=env_name):
        validate_admin_preview_config(get_settings())


@pytest.mark.unit
def test_validate_admin_preview_config_allows_preview_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    validate_admin_preview_config(get_settings())


@pytest.mark.unit
def test_admin_route_inventory_lists_current_unsafe_methods() -> None:
    inventory = _admin_unsafe_route_methods(app)
    paths = {path for path, _ in inventory}
    assert "/admin/login" in paths
    assert "/admin/companies" in paths
    assert "/admin/briefs/{brief_id}/convert" in paths
    assert "/admin/api/imports/linkedin/commit" in paths
    assert "/admin/pipeline/{company_id}/stage" in paths
    for _, methods in inventory:
        assert methods.issubset(set(_UNSAFE_METHODS) | {"OPTIONS"})


@pytest.mark.unit
@pytest.mark.integration
def test_preview_denies_every_registered_admin_unsafe_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    inventory = _admin_unsafe_route_methods(app)
    assert inventory, "expected at least one /admin unsafe route"
    for path, methods in inventory:
        sample_path = _sample_admin_path(path)
        for method in sorted(methods):
            if method == "OPTIONS":
                continue
            response = client.request(method, sample_path, content=b"ignored-body")
            assert response.status_code == 405, f"{method} {sample_path}"
            assert response.headers.get("allow") == "GET, HEAD"


@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.parametrize(
    "content_type,body",
    [
        ("application/json", b'{"records": [{"full_name": "Ada"}]}'),
        ("application/x-www-form-urlencoded", b"csrf_token=x&name=Acme"),
        (
            "multipart/form-data; boundary=----preview",
            b"------preview\r\nContent-Disposition: form-data; name=\"file\"; "
            b'filename="big.csv"\r\nContent-Type: text/csv\r\n\r\n' + (b"x" * 4096)
            + b"\r\n------preview--\r\n",
        ),
        ("text/plain", b"not-json{" * 200),
    ],
)
def test_preview_denies_admin_post_bodies_without_db_access(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    body: bytes,
) -> None:
    _preview_env(monkeypatch)
    db_called = False

    def _forbidden_db(*args: Any, **kwargs: Any) -> Iterator[MagicMock]:
        nonlocal db_called
        db_called = True
        yield MagicMock()

    with patch("app.db.db_connection", side_effect=_forbidden_db):
        response = client.post(
            "/admin/companies",
            content=body,
            headers={"Content-Type": content_type},
            cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
        )
    assert response.status_code == 405
    assert response.headers.get("allow") == "GET, HEAD"
    assert db_called is False


@pytest.mark.unit
@pytest.mark.integration
def test_preview_mutation_routes_have_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    db_called = False
    stripe_called = False
    email_called = False

    def _forbidden_db(*args: Any, **kwargs: Any) -> Iterator[MagicMock]:
        nonlocal db_called
        db_called = True
        yield MagicMock()

    def _forbidden_stripe(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal stripe_called
        stripe_called = True
        return MagicMock()

    def _forbidden_email(*args: Any, **kwargs: Any) -> None:
        nonlocal email_called
        email_called = True

    mutation_targets = [
        ("POST", "/admin/login", b"username=a&password=b&csrf_token=x"),
        ("POST", "/admin/logout", b"csrf_token=x"),
        ("POST", "/admin/companies", b"name=Acme"),
        ("POST", "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit", b"name=Acme"),
        ("POST", "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/archive", b"csrf=x"),
        ("POST", "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/restore", b"csrf=x"),
        ("POST", "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/research", b"url=x"),
        ("POST", "/admin/contacts", b"full_name=Ada"),
        ("POST", "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/edit", b"full_name=Ada"),
        ("POST", "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/archive", b"csrf=x"),
        ("POST", "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/restore", b"csrf=x"),
        ("POST", "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/research", b"url=x"),
        ("POST", "/admin/briefs/4/convert", b"company_choice=new&contact_choice=new"),
        (
            "POST",
            "/admin/pipeline/11111111-1111-1111-1111-111111111111/stage",
            b"to_stage=won",
        ),
        (
            "POST",
            "/admin/pipeline/11111111-1111-1111-1111-111111111111/next-action",
            b"next_action=Follow",
        ),
        (
            "POST",
            "/admin/pipeline/11111111-1111-1111-1111-111111111111/activities",
            b"activity_type=note&summary=Hi",
        ),
        (
            "POST",
            "/admin/imports/batches/11111111-1111-1111-1111-111111111111/rollback",
            b"csrf=x",
        ),
        (
            "POST",
            "/admin/api/imports/linkedin/commit",
            b'{"schema_version":"linkedin_export_v1","records":[]}',
        ),
    ]

    with (
        patch("app.db.db_connection", side_effect=_forbidden_db),
        patch("app.stripe_service.create_checkout_session", side_effect=_forbidden_stripe),
        patch("app.email_service.notify_team_of_new_brief", side_effect=_forbidden_email),
    ):
        for method, path, body in mutation_targets:
            response = client.request(
                method,
                path,
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                cookies={SESSION_COOKIE_NAME: PREVIEW_SESSION_TOKEN},
            )
            assert response.status_code == 405, f"{method} {path}"

    assert db_called is False
    assert stripe_called is False
    assert email_called is False


@pytest.mark.unit
@pytest.mark.integration
def test_preview_get_renders_fixture_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Overdue next actions" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_head_is_not_blocked_by_read_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _preview_env(monkeypatch)
    response = client.head("/admin")
    assert response.headers.get("allow") != "GET, HEAD"


@pytest.mark.unit
@pytest.mark.integration
def test_preview_disabled_mutations_are_not_blocked_by_read_only_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    response = client.post(
        "/admin/login",
        data={"username": "x", "password": "y", "csrf_token": "z"},
    )
    assert response.headers.get("allow") != "GET, HEAD"
    assert response.status_code != 405


@pytest.mark.unit
def test_preview_disabled_on_production_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    assert get_settings().admin_preview_enabled is False


@pytest.mark.unit
@pytest.mark.integration
def test_preview_not_enabled_via_forwarded_host_on_production_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get_settings().admin_preview_enabled is False
    response = client.post(
        "/admin/companies",
        data={"name": "Acme"},
        headers={"X-Forwarded-Host": "127.0.0.1:8765"},
    )
    assert response.headers.get("allow") != "GET, HEAD"


@pytest.mark.unit
def test_preview_startup_fails_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    for key in PREVIEW_CLEARED_SECRETS:
        if key != "DATABASE_URL":
            monkeypatch.delenv(key, raising=False)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = build_preview_child_env(port=port, parent_environ=os.environ)
    env["DATABASE_URL"] = "postgresql://prod/db"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
        pytest.fail("preview server stayed alive with DATABASE_URL configured")
    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    assert proc.returncode != 0
    assert "ADMIN_PREVIEW_MODE" in stderr or "DATABASE_URL" in stderr
