"""Integration tests for admin brief-to-pipeline conversion routes (#148)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.admin_auth import SESSION_COOKIE_NAME
from app.brief_conversion import BriefConversionValidationError
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!"
CSRF_TOKEN = "csrf-convert-token"

COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.admin_routes.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _session_row(*, token_hash: str) -> dict[str, Any]:
    return {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": admin_auth.hash_csrf_token(CSRF_TOKEN),
    }


def _detail_brief() -> dict[str, Any]:
    return {
        "id": 42,
        "created_at": datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        "website": "https://acme.example",
        "contact_method": "email",
        "contact_value": "ops@acme.example",
        "brief": "Need architecture review.",
        "status": "paid",
        "stripe_session_id": "cs_test",
        "stripe_payment_intent_id": "pi_test",
        "paid_at": datetime(2026, 7, 14, 10, 45, tzinfo=timezone.utc),
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
        "utm_content": None,
        "utm_term": None,
    }


def _fake_session() -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=1,
        admin_username=TEST_USERNAME,
        token_hash="session-hash",
        csrf_token_hash=admin_auth.hash_csrf_token(CSRF_TOKEN),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.mark.unit
@pytest.mark.integration
def test_convert_preview_requires_auth() -> None:
    response = client.get("/admin/briefs/42/convert")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_convert_post_requires_auth_and_csrf() -> None:
    unauth = client.post("/admin/briefs/42/convert", data={"csrf_token": CSRF_TOKEN})
    assert unauth.status_code == 303

    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        bad_csrf = client.post(
            "/admin/briefs/42/convert",
            data={"csrf_token": "wrong", "company_choice": "new", "contact_choice": "new"},
        )
    assert bad_csrf.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_brief_detail_shows_add_to_pipeline_when_available() -> None:
    token_hash = admin_auth.hash_session_token("detail-convert")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                with patch("app.admin_routes._crm") as crm:
                    crm.get_project_brief_source.return_value = None
                    with patch(
                        "app.admin_routes._session_csrf_for_forms",
                        return_value=CSRF_TOKEN,
                    ):
                        response = client.get(
                            "/admin/briefs/42",
                            cookies={SESSION_COOKIE_NAME: "detail-convert"},
                        )
    assert response.status_code == 200
    assert "Add to pipeline" in response.text
    crm.get_project_brief_source.assert_called_with(conn, 42)


@pytest.mark.unit
@pytest.mark.integration
def test_brief_detail_shows_linked_records_when_converted() -> None:
    token_hash = admin_auth.hash_session_token("detail-linked")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                with patch("app.admin_routes._crm") as crm:
                    crm.get_project_brief_source.return_value = {"external_id": "42"}
                    crm.get_brief_conversion_state.return_value = {
                        "company": {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "diagnostic_paid"},
                        "contact": {"id": CONTACT_ID, "email": "ops@acme.example"},
                        "pipeline_stage": "diagnostic_paid",
                    }
                    with patch(
                        "app.admin_routes._session_csrf_for_forms",
                        return_value=CSRF_TOKEN,
                    ):
                        response = client.get(
                            "/admin/briefs/42",
                            cookies={SESSION_COOKIE_NAME: "detail-linked"},
                        )
    assert response.status_code == 200
    assert "Pipeline linkage" in response.text
    assert "Diagnostic paid" in response.text
    assert f'href="/admin/companies/{COMPANY_ID}"' in response.text
    assert f'href="/admin/contacts/{CONTACT_ID}"' in response.text
    assert 'href="/admin/pipeline"' in response.text
    assert "Add to pipeline" not in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_convert_preview_renders_proposed_fields_and_matches() -> None:
    token_hash = admin_auth.hash_session_token("convert-preview")
    row = _session_row(token_hash=token_hash)
    preview = {
        "proposal": {
            "company_name": "Acme",
            "website": "https://acme.example",
            "domain": "acme.example",
            "contact_email": "ops@acme.example",
            "pipeline_stage_label": "Diagnostic paid",
            "brief_status": "paid",
            "expected_value": 200.0,
        },
        "company_matches": [{"id": COMPANY_ID, "name": "Acme Existing", "domain": "acme.example"}],
        "contact_matches": [],
    }
    with mock_db_connection() as conn:
        with patch("app.admin_routes.db.get_admin_session_by_token_hash", return_value=row):
            with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                with patch("app.admin_routes._crm") as crm:
                    crm.get_project_brief_source.return_value = None
                    crm.find_brief_conversion_matches.return_value = preview
                    with patch(
                        "app.admin_routes._session_csrf_for_forms",
                        return_value=CSRF_TOKEN,
                    ):
                        response = client.get(
                            "/admin/briefs/42/convert",
                            cookies={SESSION_COOKIE_NAME: "convert-preview"},
                        )
    assert response.status_code == 200
    assert "Add brief #42 to pipeline" in response.text
    assert "Diagnostic paid" in response.text
    assert "Acme Existing" in response.text
    crm.find_brief_conversion_matches.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_convert_post_success_redirects_with_converted_flag() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection() as conn:
                with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                    with patch("app.admin_routes._crm") as crm:
                        crm.convert_project_brief.return_value = {"idempotent": False}
                        response = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "new",
                                "contact_choice": "new",
                            },
                        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/briefs/42?converted=1"
    crm.convert_project_brief.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_convert_post_validation_error_returns_to_preview() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection():
                with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                    with patch("app.admin_routes._crm") as crm:
                        crm.convert_project_brief.side_effect = BriefConversionValidationError(
                            "Select an existing company match or choose to create a new company."
                        )
                        response = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "existing",
                                "contact_choice": "new",
                            },
                        )
    assert response.status_code == 303
    assert "/admin/briefs/42/convert?error=" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_convert_post_is_idempotent_on_repeat_submission() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection():
                with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                    with patch("app.admin_routes._crm") as crm:
                        crm.convert_project_brief.return_value = {"idempotent": True}
                        first = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "new",
                                "contact_choice": "new",
                            },
                        )
                        second = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "new",
                                "contact_choice": "new",
                            },
                        )
    assert first.status_code == 303
    assert second.status_code == 303
    assert crm.convert_project_brief.call_count == 2


@pytest.mark.unit
@pytest.mark.integration
def test_convert_get_redirects_when_brief_already_linked() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with mock_db_connection() as conn:
            with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                with patch("app.admin_routes._crm") as crm:
                    crm.get_project_brief_source.return_value = {"external_id": "42"}
                    response = client.get("/admin/briefs/42/convert")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/briefs/42?converted=1"
    crm.find_brief_conversion_matches.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_convert_get_not_found_for_missing_brief() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with mock_db_connection():
            with patch("app.admin_routes.brief_service.get_brief", return_value=None):
                response = client.get("/admin/briefs/42/convert")
    assert response.status_code == 404
    assert "Brief not found" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_convert_get_renders_matches_for_unconverted_brief() -> None:
    preview = {
        "proposal": {"company_name": "Acme", "pipeline_stage_label": "Qualified"},
        "company_matches": [],
        "contact_matches": [],
    }
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with mock_db_connection():
            with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                with patch("app.admin_routes._crm") as crm:
                    crm.get_project_brief_source.return_value = None
                    crm.find_brief_conversion_matches.return_value = preview
                    with patch(
                        "app.admin_routes._session_csrf_for_forms",
                        return_value=CSRF_TOKEN,
                    ):
                        response = client.get("/admin/briefs/42/convert")
    assert response.status_code == 200
    assert "Proposed records" in response.text
    crm.find_brief_conversion_matches.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_preview_mode_convert_pages_use_mock_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN):
            detail = client.get("/admin/briefs/1")
            convert_page = client.get("/admin/briefs/4/convert")
            converted = client.get("/admin/briefs/3")
    assert detail.status_code == 200
    assert "Add to pipeline" in detail.text
    assert convert_page.status_code == 200
    assert "Proposed records" in convert_page.text
    assert converted.status_code == 200
    assert "Pipeline linkage" in converted.text
    assert 'href="/admin/contacts/' in converted.text
    assert 'href="/admin/pipeline"' in converted.text


@pytest.mark.unit
@pytest.mark.integration
def test_preview_convert_validation_error_renders_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN):
            response = client.get("/admin/briefs/4/convert?error=validation")
    assert response.status_code == 200
    assert "form-error" in response.text
    assert "Select an existing company match" in response.text
