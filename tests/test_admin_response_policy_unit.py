"""Unit tests for admin response security policy (#308)."""

from __future__ import annotations

import re

import pytest

from app.admin_response_policy import (
    ADMIN_CACHE_CONTROL,
    CSP_ENFORCEMENT_DEADLINE,
    CSP_ENFORCEMENT_OWNER,
    _REQUIRED_CSP_DIRECTIVE_NAMES,
    admin_cache_headers,
    admin_security_headers,
    apply_admin_cache_headers,
    build_admin_csp,
    generate_csp_nonce,
    hsts_enabled,
    nonce_entropy_bits,
    parse_csp_directives,
    validate_admin_csp,
)
from app.app_environment import AppEnvironment
from app.config import Settings
from starlette.responses import Response


def _settings(*, base_url: str = "http://localhost:8000") -> Settings:
    return Settings(
        database_url="",
        stripe_secret_key="",
        stripe_webhook_secret="",
        stripe_publishable_key="",
        resend_api_key="",
        from_email="noreply@saberistic.com",
        notify_email="inbox@saberistic.com",
        base_url=base_url,
        analytics_environment="development",
        app_environment=AppEnvironment.DEVELOPMENT,
        admin_username="",
        admin_password_hash="",
        admin_session_secret="",
    )


@pytest.mark.unit
def test_generate_csp_nonce_has_at_least_128_bits() -> None:
    nonce = generate_csp_nonce()
    assert nonce_entropy_bits(nonce) >= 128


@pytest.mark.unit
def test_generate_csp_nonce_is_unpredictable() -> None:
    a = generate_csp_nonce()
    b = generate_csp_nonce()
    assert a != b


@pytest.mark.unit
def test_build_admin_csp_includes_required_directives() -> None:
    nonce = generate_csp_nonce()
    policy = build_admin_csp(nonce=nonce)
    parsed = parse_csp_directives(policy)
    assert set(parsed) == set(_REQUIRED_CSP_DIRECTIVE_NAMES)
    assert parsed["frame-ancestors"] == "'none'"
    assert parsed["base-uri"] == "'none'"
    assert parsed["object-src"] == "'none'"
    assert parsed["form-action"] == "'self'"
    assert f"'nonce-{nonce}'" in parsed["script-src"]
    assert "https://fonts.googleapis.com" in parsed["style-src"]
    assert "https://fonts.gstatic.com" in parsed["font-src"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_token",
    ["*", "'unsafe-eval'", "unsafe-eval", "'unsafe-inline'", "unsafe-inline"],
)
def test_validate_admin_csp_rejects_forbidden_tokens(bad_token: str) -> None:
    nonce = generate_csp_nonce()
    policy = build_admin_csp(nonce=nonce).replace(
        "script-src", f"script-src {bad_token}"
    )
    with pytest.raises(ValueError, match="disallowed CSP token"):
        validate_admin_csp(policy)


@pytest.mark.unit
def test_validate_admin_csp_requires_nonce_in_script_src() -> None:
    policy = (
        "default-src 'none'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
        "style-src 'self'; font-src 'self'; img-src 'self'; connect-src 'self'"
    )
    with pytest.raises(ValueError, match="script-src must include a nonce"):
        validate_admin_csp(policy)


@pytest.mark.unit
def test_admin_security_headers_snapshot() -> None:
    nonce = "test-nonce-value"
    headers = admin_security_headers(_settings(), nonce=nonce)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-XSS-Protection"] == "0"
    assert "camera=()" in headers["Permissions-Policy"]
    assert "microphone=()" in headers["Permissions-Policy"]
    assert "geolocation=()" in headers["Permissions-Policy"]
    assert f"'nonce-{nonce}'" in headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in headers


@pytest.mark.unit
def test_hsts_only_for_https_base_url() -> None:
    assert not hsts_enabled(_settings(base_url="http://localhost:8000"))
    assert hsts_enabled(_settings(base_url="https://saberistic.com"))


@pytest.mark.unit
def test_hsts_header_present_for_https() -> None:
    nonce = generate_csp_nonce()
    headers = admin_security_headers(
        _settings(base_url="https://saberistic.com"),
        nonce=nonce,
    )
    assert headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "includeSubDomains" not in headers["Strict-Transport-Security"]
    assert "preload" not in headers["Strict-Transport-Security"]


@pytest.mark.unit
def test_csp_enforcement_plan_is_bounded() -> None:
    assert CSP_ENFORCEMENT_OWNER
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", CSP_ENFORCEMENT_DEADLINE)


@pytest.mark.unit
def test_admin_cache_headers_constant() -> None:
    headers = admin_cache_headers()
    assert headers == {"Cache-Control": ADMIN_CACHE_CONTROL}
    assert headers["Cache-Control"] == "no-store, private"


@pytest.mark.unit
def test_apply_admin_cache_headers_replaces_weaker_directive() -> None:
    response = Response(content="ok", status_code=200)
    response.headers["Cache-Control"] = "public, max-age=3600"
    apply_admin_cache_headers(response)
    assert response.headers["Cache-Control"] == ADMIN_CACHE_CONTROL
    assert len(list(response.headers.getlist("Cache-Control"))) == 1
