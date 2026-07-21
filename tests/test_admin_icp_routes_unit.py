"""Unit tests for ICP scoring admin routes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS
from app.icp_scoring import default_icp_rules
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
COMPANY_ID = PREVIEW_PIPELINE_COMPANY_IDS[0]

_session_store: dict[str, dict[str, Any]] = {}


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
        patch("app.admin_icp_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/signals", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_anonymous_icp_scores_redirects_to_login() -> None:
    response = client.get("/admin/signals")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_icp_scores_list_renders_for_authenticated_user(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.list_company_icp_scores.return_value = [
        {
            "company_id": COMPANY_ID,
            "company_name": "Acme",
            "total_score": 7.0,
            "version_number": 1,
            "is_override": False,
            "calculated_at": datetime.now(timezone.utc),
        }
    ]
    crm.get_active_icp_version.return_value = {
        "version_number": 1,
        "label": "Default Saberistic ICP",
    }
    with patch("app.admin_icp_routes._crm", crm):
        response = client.get("/admin/signals", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "ICP scores" in response.text
    assert "Acme" in response.text
    assert "Edit rules" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_rules_page_renders_default_rules(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.get_active_icp_version.return_value = {
        "version_number": 1,
        "label": "Default Saberistic ICP",
    }
    crm.list_active_icp_rules.return_value = []
    with patch("app.admin_icp_routes._crm", crm):
        response = client.get("/admin/signals/rules", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "vertical_fit" in response.text
    assert "Publish new rule version" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_detail_renders_breakdown(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.get_company_icp_score_detail.return_value = {
        "company": {"id": str(COMPANY_ID), "name": "Acme"},
        "snapshot": {
            "total_score": 6.0,
            "computed_score": 6.0,
            "version_number": 1,
            "calculated_at": datetime.now(timezone.utc),
            "is_override": False,
            "missing_inputs": ["company.stage"],
            "breakdown": [
                {
                    "rule_id": "vertical_fit",
                    "label": "Target vertical",
                    "points_awarded": 1.0,
                    "weight": 1.0,
                    "status": "scored",
                    "missing_inputs": [],
                    "evidence": [{"field": "category"}],
                }
            ],
        },
        "active_version": {"version_number": 1},
    }
    with patch("app.admin_icp_routes._crm", crm):
        response = client.get(
            f"/admin/signals/{COMPANY_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "vertical_fit" in response.text
    assert "Recalculate score" in response.text
    assert "Manual override" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_recalculate_redirects_after_success(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    with patch("app.admin_icp_routes._crm", crm):
        response = client.post(
            f"/admin/signals/{COMPANY_ID}/recalculate",
            cookies=authenticated_admin["cookies"],
            data={"csrf_token": authenticated_admin["csrf_token"]},
        )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/admin/signals/{COMPANY_ID}")
    crm.calculate_company_icp_score.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_icp_override_requires_reason(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.override_company_icp_score.side_effect = ValueError("Override reason is required.")
    with patch("app.admin_icp_routes._crm", crm):
        response = client.post(
            f"/admin/signals/{COMPANY_ID}/override",
            cookies=authenticated_admin["cookies"],
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "override_score": "8.0",
                "reason": "   ",
            },
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_icp_scores_preview_mode_renders_mock_rows(
    authenticated_admin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get("/admin/signals", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Preview data" in response.text
    assert "ICP scores" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_rules_preview_mode_renders_defaults(
    authenticated_admin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get("/admin/signals/rules", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "vertical_fit" in response.text
    assert "Preview data" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_detail_preview_mode_renders_override(
    authenticated_admin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.admin_preview import PREVIEW_PIPELINE_COMPANY_IDS

    enable_admin_preview_env(monkeypatch)
    response = client.get(
        f"/admin/signals/{PREVIEW_PIPELINE_COMPANY_IDS[1]}",
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 200
    assert "Manual override" in response.text
    assert "Preview data" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_icp_rules_save_publishes_new_version(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    existing = [rule.model_dump() for rule in default_icp_rules()]
    crm.list_active_icp_rules.return_value = existing
    crm.publish_icp_rule_version.return_value = {"version": {"version_number": 2}}
    form_data = {
        "csrf_token": authenticated_admin["csrf_token"],
    }
    for rule in default_icp_rules():
        form_data[f"dimension__{rule.id}"] = rule.dimension
        form_data[f"label__{rule.id}"] = rule.label
        form_data[f"weight__{rule.id}"] = str(rule.weight)
        form_data[f"enabled__{rule.id}"] = "on"
    with patch("app.admin_icp_routes._crm", crm):
        response = client.post(
            "/admin/signals/rules",
            cookies=authenticated_admin["cookies"],
            data=form_data,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/signals/rules"
    crm.publish_icp_rule_version.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_icp_recalculate_value_error_redirects_with_error(
    authenticated_admin: dict[str, Any],
) -> None:
    crm = MagicMock()
    crm.calculate_company_icp_score.side_effect = ValueError("Company not found.")
    with patch("app.admin_icp_routes._crm", crm):
        response = client.post(
            f"/admin/signals/{COMPANY_ID}/recalculate",
            cookies=authenticated_admin["cookies"],
            data={"csrf_token": authenticated_admin["csrf_token"]},
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
