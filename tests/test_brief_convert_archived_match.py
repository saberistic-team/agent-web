"""Tests for archived contact identity matches on brief conversion preview (#276)."""

from __future__ import annotations

import re
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
from app.admin_pages import render_admin_brief_convert_page
from app.brief_conversion import BriefConversionValidationError
from app.brief_service import BriefListFilters
from app.crm_service import CrmService
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
CSRF_TOKEN = "csrf-archived-convert"

ARCHIVED_CONTACT_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeee01")
NEW_CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

pytestmark = [pytest.mark.unit, pytest.mark.integration]


def _back_filters() -> BriefListFilters:
    return BriefListFilters(
        page=1,
        per_page=20,
        query=None,
        status=None,
        date_from=None,
        date_to=None,
        date_from_raw=None,
        date_to_raw=None,
    )


def _archived_match() -> dict[str, Any]:
    return {
        "id": ARCHIVED_CONTACT_ID,
        "full_name": "Jordan Lee",
        "email": "ops@acme.example",
        "company_name": "Acme Corp",
        "archived_at": "2026-01-15T12:00:00+00:00",
    }


def _preview(*, contact_matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "proposal": {
            "company_name": "Acme",
            "website": "https://acme.example",
            "domain": "acme.example",
            "contact_email": "ops@acme.example",
            "pipeline_stage_label": "Diagnostic paid",
            "brief_status": "paid",
        },
        "company_matches": [],
        "contact_matches": contact_matches or [],
        "archived_contact_match": _archived_match(),
    }


@pytest.mark.unit
def test_renderer_shows_archived_contact_panel_with_review_links() -> None:
    html = render_admin_brief_convert_page(
        admin_username="operator",
        brief={"id": 42, "status": "paid"},
        back_filters=_back_filters(),
        preview=_preview(),
        csrf_token="csrf",
    )
    assert "Archived contact match" in html
    assert "Jordan Lee" in html
    assert "ops@acme.example" in html
    assert "Acme Corp" in html
    assert "2026-01-15 12:00:00 UTC" in html
    assert "never linked as active contacts automatically" in html
    assert f'href="/admin/contacts/{ARCHIVED_CONTACT_ID}"' in html
    assert f'href="/admin/contacts/{ARCHIVED_CONTACT_ID}/edit"' in html
    assert "Review archived contact" in html
    assert "Restore archived contact" in html


@pytest.mark.unit
def test_renderer_does_not_preselect_create_new_when_archived_only() -> None:
    html = render_admin_brief_convert_page(
        admin_username="operator",
        brief={"id": 42, "status": "paid"},
        back_filters=_back_filters(),
        preview=_preview(),
        csrf_token="csrf",
    )
    assert re.search(
        r'name="contact_choice" value="new"\s+required',
        html,
    )
    assert 'name="contact_choice" value="new" checked' not in html


@pytest.mark.unit
def test_renderer_hides_archived_panel_when_active_contact_match_present() -> None:
    html = render_admin_brief_convert_page(
        admin_username="operator",
        brief={"id": 42, "status": "paid"},
        back_filters=_back_filters(),
        preview=_preview(
            contact_matches=[{"id": NEW_CONTACT_ID, "email": "ops@acme.example"}],
        ),
        csrf_token="csrf",
    )
    assert "Archived contact match" not in html
    assert 'name="contact_choice" value="existing:' in html


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


@pytest.mark.integration
def test_convert_preview_route_renders_archived_only_match() -> None:
    preview = _preview()
    token_hash = admin_auth.hash_session_token("archived-preview")
    row = _session_row(token_hash=token_hash)
    with mock_db_connection():
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
                            cookies={SESSION_COOKIE_NAME: "archived-preview"},
                        )
    assert response.status_code == 200
    assert "Archived contact match" in response.text
    assert f"/admin/contacts/{ARCHIVED_CONTACT_ID}/edit" in response.text


@pytest.mark.integration
def test_convert_post_requires_explicit_contact_choice_with_archived_match() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection():
                with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                    with patch("app.admin_routes._crm") as crm:
                        crm.convert_project_brief.side_effect = BriefConversionValidationError(
                            "Choose whether to create a new contact or review the archived match first."
                        )
                        response = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "new",
                            },
                        )
    assert response.status_code == 303
    assert "archived%20match" in response.headers["location"]


@pytest.mark.integration
def test_convert_post_allows_explicit_new_contact_with_archived_history() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection():
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


@pytest.mark.integration
def test_convert_post_rejects_stale_new_choice_when_active_contact_appears() -> None:
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._verify_session_csrf"):
            with mock_db_connection():
                with patch("app.admin_routes.brief_service.get_brief", return_value=_detail_brief()):
                    with patch("app.admin_routes._crm") as crm:
                        crm.convert_project_brief.side_effect = BriefConversionValidationError(
                            "A contact with this email already exists — link the existing contact."
                        )
                        response = client.post(
                            "/admin/briefs/42/convert",
                            data={
                                "csrf_token": CSRF_TOKEN,
                                "company_choice": "new",
                                "contact_choice": "new",
                            },
                        )
    assert response.status_code == 303
    assert "already%20exists" in response.headers["location"]


@pytest.mark.unit
def test_service_rejects_missing_contact_choice_when_archived_match_exists() -> None:
    from app.actor_context import ActorContext
    from app.crm_service import CrmRepositories, CrmService

    repos = {
        "companies": MagicMock(),
        "contacts": MagicMock(),
        "source_records": MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": MagicMock(),
    }
    service = CrmService(repos=CrmRepositories(**repos))
    conn = MagicMock()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_active_by_email.return_value = None
    repos["contacts"].get_archived_by_email.return_value = _archived_match()

    with pytest.raises(
        BriefConversionValidationError,
        match="review the archived match first",
    ):
        service.convert_project_brief(
            conn,
            brief={
                "id": 42,
                "website": "https://acme.example",
                "contact_value": "ops@acme.example",
                "status": "paid",
            },
            actor_context=ActorContext(actor="operator", correlation_id="corr"),
            price_cents=20_000,
            company_choice="new",
            contact_choice="",
        )


@pytest.mark.integration
def test_preview_archived_only_convert_page_renders_mock_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import PREVIEW_BRIEF_CONVERT_ARCHIVED_ONLY_ID

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN):
            response = client.get(
                f"/admin/briefs/{PREVIEW_BRIEF_CONVERT_ARCHIVED_ONLY_ID}/convert",
            )
    assert response.status_code == 200
    assert "Archived contact match" in response.text
    assert "Jordan Lee (archived)" in response.text


@pytest.mark.integration
def test_preview_active_only_convert_page_has_no_archived_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import PREVIEW_BRIEF_CONVERT_ACTIVE_ONLY_ID

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("app.admin_routes.require_admin_session", return_value=_fake_session()):
        with patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN):
            response = client.get(
                f"/admin/briefs/{PREVIEW_BRIEF_CONVERT_ACTIVE_ONLY_ID}/convert",
            )
    assert response.status_code == 200
    assert 'name="contact_choice"' in response.text
    assert "existing:dddddddd-dddd-dddd-dddd-dddddddddddd" in response.text
    assert "Archived contact match" not in response.text
