"""Unit tests for pipeline admin routes."""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.acquisition_pipeline import (
    EXPECTED_VALUE_CENTS_INVALID_MSG,
    PipelineTransitionError,
)
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS
from app.config import get_settings
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_session_store: dict[str, dict[str, Any]] = {}


def _pipeline_company() -> dict[str, Any]:
    return {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
        "next_action": "Old action",
        "next_action_due_at": datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        "pipeline_owner": "operator",
        "expected_value_cents": 50_000,
    }


def _post_next_action(
    authenticated_admin: dict[str, Any],
    *,
    crm: MagicMock,
    data: dict[str, str],
) -> Any:
    with patch("app.admin_pipeline_routes._crm", crm):
        return client.post(
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            data={"csrf_token": authenticated_admin["csrf_token"], **data},
            cookies=authenticated_admin["cookies"],
        )


def _settings_without_database() -> Any:
    """Real settings with database_url cleared, for no-DB branch tests.

    Auth (`require_admin_session`) still resolves via the unmodified
    `app.admin_routes.get_settings`, so only patch this module's binding.
    """
    return dataclasses.replace(get_settings(), database_url="")


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> dict[str, Any]:
    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    _session_store[token_hash] = {
        "id": 1,
        "token_hash": token_hash,
        "admin_username": TEST_USERNAME,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked_at": None,
        "csrf_token_hash": csrf_hash,
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return _session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in _session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
        patch("app.admin_pipeline_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/pipeline", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_pipeline_redirects_to_login() -> None:
    response = client.get("/admin/pipeline")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_list_renders_for_authenticated_user(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.list_pipeline_companies.return_value = [
        {
            "id": COMPANY_ID,
            "name": "Acme",
            "pipeline_stage": "qualified",
            "expected_value_cents": 50_000,
            "next_action": "Call founder",
            "next_action_due_at": datetime.now(timezone.utc),
            "pipeline_owner": "operator",
        }
    ]
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get("/admin/pipeline", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Acme" in response.text
    assert "Qualified" in response.text


@pytest.mark.unit
def test_pipeline_stage_change_requires_csrf(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        f"/admin/pipeline/{COMPANY_ID}/stage",
        data={"csrf_token": "bad", "to_stage": "qualified"},
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_stage_change_success_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "to_stage": "qualified",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/pipeline/{COMPANY_ID}"
    crm.transition_pipeline_stage.assert_called_once()


@pytest.mark.unit
def test_pipeline_stage_change_shows_transition_error(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.transition_pipeline_stage.side_effect = PipelineTransitionError("blocked")
    crm.get_pipeline_company.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
    }
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "to_stage": "won",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 400
    assert "blocked" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_preview_mode_returns_mock_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    response = client.get("/admin/pipeline")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Northwind Labs" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_pipeline_detail_renders(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "pipeline_stage": "researching",
    }
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get(
            f"/admin/pipeline/{COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Change stage" in response.text


@pytest.mark.unit
def test_pipeline_next_action_update_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={"next_action": "Follow up", "expected_value_cents": "50000"},
    )
    assert response.status_code == 303
    crm.update_pipeline_next_action.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expected_value_cents", "expected_parsed"),
    [
        ("", None),
        ("0", 0),
        ("50000", 50_000),
        ("  75000  ", 75_000),
    ],
)
def test_pipeline_next_action_valid_expected_value_updates(
    authenticated_admin: dict[str, Any],
    expected_value_cents: str,
    expected_parsed: int | None,
) -> None:
    crm = MagicMock()
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={"expected_value_cents": expected_value_cents},
    )
    assert response.status_code == 303
    update = crm.update_pipeline_next_action.call_args.kwargs["update"]
    assert update.expected_value_cents == expected_parsed


@pytest.mark.unit
@pytest.mark.parametrize(
    "expected_value_cents",
    ["abc", "12.5", "-1", "2147483648", "999999999999999999999"],
)
def test_pipeline_next_action_invalid_expected_value_redisplays_form(
    authenticated_admin: dict[str, Any],
    expected_value_cents: str,
) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = _pipeline_company()
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={
            "next_action": "Follow up",
            "next_action_due_at": "2026-08-01T09:30",
            "pipeline_owner": "Pat",
            "expected_value_cents": expected_value_cents,
        },
    )
    assert response.status_code == 400
    assert EXPECTED_VALUE_CENTS_INVALID_MSG in response.text
    assert 'id="expected_value_cents-error"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert f'value="{expected_value_cents}"' in response.text
    assert "Follow up" in response.text
    assert "Pat" in response.text
    assert 'value="2026-08-01T09:30"' in response.text
    crm.update_pipeline_next_action.assert_not_called()


@pytest.mark.unit
def test_pipeline_next_action_repeated_invalid_submission_does_not_write(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = _pipeline_company()
    crm.list_pipeline_stage_history.return_value = []
    crm._repos.activities.list_for_company.return_value = []
    data = {"expected_value_cents": "abc"}
    for _ in range(2):
        response = _post_next_action(authenticated_admin, crm=crm, data=data)
        assert response.status_code == 400
        assert EXPECTED_VALUE_CENTS_INVALID_MSG in response.text
    crm.update_pipeline_next_action.assert_not_called()


@pytest.mark.unit
def test_pipeline_preview_detail_validation_error_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    company_id = PREVIEW_PIPELINE_COMPANY_IDS[0]
    response = client.get(
        f"/admin/pipeline/{company_id}?error=validation&focus=expected_value_cents"
    )
    assert response.status_code == 200
    assert EXPECTED_VALUE_CENTS_INVALID_MSG in response.text
    assert 'id="expected_value_cents-error"' in response.text
    assert 'value="abc"' in response.text


@pytest.mark.unit
def test_pipeline_activity_post_redirects(authenticated_admin: dict[str, Any]) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/activities",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "activity_type": "note",
                "summary": "Left voicemail",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    crm.record_pipeline_activity.assert_called_once()


@pytest.mark.unit
def test_pipeline_preview_detail_fixed_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    company_id = PREVIEW_PIPELINE_COMPANY_IDS[0]
    response = client.get(f"/admin/pipeline/{company_id}")
    assert response.status_code == 200
    assert "Stage history" in response.text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("due_at_raw", "expect_naive_normalized"),
    [
        ("2026-08-01T09:30", True),
        ("2026-08-01T09:30:00+00:00", False),
    ],
)
def test_pipeline_next_action_due_at_is_parsed_to_aware_datetime(
    authenticated_admin: dict[str, Any],
    due_at_raw: str,
    expect_naive_normalized: bool,
) -> None:
    """Exercise `_parse_due_at`'s non-blank path for both naive and aware ISO input."""
    crm = MagicMock()
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={"next_action_due_at": due_at_raw},
    )
    assert response.status_code == 303
    update = crm.update_pipeline_next_action.call_args.kwargs["update"]
    assert update.next_action_due_at is not None
    assert update.next_action_due_at.tzinfo is not None
    if expect_naive_normalized:
        assert update.next_action_due_at.tzinfo == timezone.utc


@pytest.mark.unit
def test_pipeline_list_without_database_configured_renders_empty(
    authenticated_admin: dict[str, Any],
) -> None:
    """Non-preview, no DB: the `if settings.database_url:` guard short-circuits."""
    with patch(
        "app.admin_pipeline_routes.get_settings",
        return_value=_settings_without_database(),
    ):
        response = client.get("/admin/pipeline", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Acme" not in response.text


@pytest.mark.unit
def test_pipeline_list_swallows_crm_lookup_exception(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.list_pipeline_companies.side_effect = RuntimeError("boom")
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get("/admin/pipeline", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Acme" not in response.text


@pytest.mark.unit
def test_pipeline_preview_detail_unknown_id_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    unknown_id = UUID("99999999-9999-9999-9999-999999999999")
    response = client.get(f"/admin/pipeline/{unknown_id}")
    assert response.status_code == 404


@pytest.mark.unit
def test_pipeline_detail_without_database_configured_returns_503(
    authenticated_admin: dict[str, Any],
) -> None:
    with patch(
        "app.admin_pipeline_routes.get_settings",
        return_value=_settings_without_database(),
    ):
        response = client.get(
            f"/admin/pipeline/{COMPANY_ID}", cookies=authenticated_admin["cookies"]
        )
    assert response.status_code == 503


@pytest.mark.unit
def test_pipeline_detail_missing_company_returns_404(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = None
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.get(
            f"/admin/pipeline/{COMPANY_ID}", cookies=authenticated_admin["cookies"]
        )
    assert response.status_code == 404


@pytest.mark.unit
def test_pipeline_stage_change_preview_mode_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    with patch("app.admin_routes._verify_session_csrf"):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={"csrf_token": "irrelevant-in-preview", "to_stage": "qualified"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/pipeline/{COMPANY_ID}"


@pytest.mark.unit
def test_pipeline_stage_change_without_database_configured_returns_503(
    authenticated_admin: dict[str, Any],
) -> None:
    with patch(
        "app.admin_pipeline_routes.get_settings",
        return_value=_settings_without_database(),
    ):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={"csrf_token": authenticated_admin["csrf_token"], "to_stage": "qualified"},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 503


@pytest.mark.unit
def test_pipeline_stage_change_invalid_stage_returns_400(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "to_stage": "not-a-real-stage",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 400
    crm.transition_pipeline_stage.assert_not_called()


@pytest.mark.unit
def test_pipeline_stage_change_transition_error_missing_company_returns_404(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.transition_pipeline_stage.side_effect = PipelineTransitionError("blocked")
    crm.get_pipeline_company.return_value = None
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/stage",
            data={"csrf_token": authenticated_admin["csrf_token"], "to_stage": "won"},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 404


@pytest.mark.unit
def test_pipeline_next_action_preview_mode_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    with patch("app.admin_routes._verify_session_csrf"):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            data={"csrf_token": "irrelevant-in-preview", "next_action": "Follow up"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/pipeline/{COMPANY_ID}"


@pytest.mark.unit
def test_pipeline_next_action_without_database_configured_returns_503(
    authenticated_admin: dict[str, Any],
) -> None:
    with patch(
        "app.admin_pipeline_routes.get_settings",
        return_value=_settings_without_database(),
    ):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/next-action",
            data={"csrf_token": authenticated_admin["csrf_token"]},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 503


@pytest.mark.unit
def test_pipeline_next_action_unrelated_validation_error_returns_400(
    authenticated_admin: dict[str, Any],
) -> None:
    """A ValidationError not tied to expected_value_cents re-raises as a plain 400."""
    crm = MagicMock()
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={"pipeline_owner": "x" * 201},
    )
    assert response.status_code == 400
    crm.update_pipeline_next_action.assert_not_called()


@pytest.mark.unit
def test_pipeline_next_action_invalid_value_missing_company_returns_404(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.get_pipeline_company.return_value = None
    response = _post_next_action(
        authenticated_admin,
        crm=crm,
        data={"expected_value_cents": "abc"},
    )
    assert response.status_code == 404


@pytest.mark.unit
def test_pipeline_activity_preview_mode_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    with patch("app.admin_routes._verify_session_csrf"):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/activities",
            data={
                "csrf_token": "irrelevant-in-preview",
                "activity_type": "note",
                "summary": "Left voicemail",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/pipeline/{COMPANY_ID}"


@pytest.mark.unit
def test_pipeline_activity_without_database_configured_returns_503(
    authenticated_admin: dict[str, Any],
) -> None:
    with patch(
        "app.admin_pipeline_routes.get_settings",
        return_value=_settings_without_database(),
    ):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/activities",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "activity_type": "note",
                "summary": "Left voicemail",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 503


@pytest.mark.unit
def test_pipeline_activity_invalid_type_returns_400(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    with patch("app.admin_pipeline_routes._crm", crm):
        response = client.post(
            f"/admin/pipeline/{COMPANY_ID}/activities",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "activity_type": "not-a-real-type",
                "summary": "Left voicemail",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 400
    crm.record_pipeline_activity.assert_not_called()
