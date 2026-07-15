"""Tests for admin import batch pages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth, db
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_preview import PREVIEW_IMPORT_BATCH_IDS, build_preview_import_batch_detail
from app.main import app

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"

_session_store: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    _session_store.clear()


@pytest.fixture
def authenticated_admin() -> Generator[dict[str, str], None, None]:
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
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        yield {SESSION_COOKIE_NAME: raw_token}


@pytest.mark.unit
@pytest.mark.integration
def test_import_batches_requires_auth() -> None:
    response = client.get("/admin/imports/batches")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


@pytest.mark.unit
@pytest.mark.integration
def test_import_batches_preview_lists_mock_batches(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    response = client.get("/admin/imports/batches", cookies=authenticated_admin)
    assert response.status_code == 200
    body = response.text
    assert "Import batches" in body
    assert "Preview data — not production" in body
    assert "linkedin_export_v1" in body
    assert "preview-chec" in body


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_detail_preview_shows_outcomes(
    authenticated_admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "110")
    batch_id = PREVIEW_IMPORT_BATCH_IDS[0]
    response = client.get(f"/admin/imports/batches/{batch_id}", cookies=authenticated_admin)
    assert response.status_code == 200
    body = response.text
    detail = build_preview_import_batch_detail(str(batch_id))
    assert detail is not None
    for outcome in ("inserted", "updated", "unchanged", "skipped", "conflicted"):
        assert outcome in body
    assert re.search(r"linkedin\.com/in/", body)


@pytest.mark.unit
@pytest.mark.integration
def test_import_batches_lists_from_crm(
    authenticated_admin: dict[str, str],
) -> None:
    batch = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source_type": "linkedin",
        "export_date": "2026-01-15",
        "schema_version": "linkedin_export_v1",
        "checksum": "abc1234567890",
        "actor": "operator",
        "status": "committed",
        "summary_counts": {"inserted": 3, "updated": 0, "unchanged": 0, "skipped": 0, "conflicted": 0},
        "created_at": "2026-01-16T00:00:00+00:00",
    }
    with patch("app.admin_routes._crm.list_import_batches", return_value=([batch], 1)):
        response = client.get("/admin/imports/batches", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Import batches" in response.text
    assert "inserted 3" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_detail_from_crm_and_missing(
    authenticated_admin: dict[str, str],
) -> None:
    batch_id = "11111111-1111-1111-1111-111111111111"
    state = {
        "batch": {
            "id": batch_id,
            "source_type": "linkedin",
            "export_date": "2026-01-15",
            "schema_version": "linkedin_export_v1",
            "checksum": "abc123",
            "actor": "operator",
            "status": "committed",
            "summary_counts": {"inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0, "conflicted": 0},
            "correlation_id": "corr-1",
        },
        "rows": [
            {
                "row_index": 0,
                "outcome": "inserted",
                "source_identity": {"full_name": "Ada", "profile_url": "https://linkedin.com/in/ada"},
                "entity_type": "contact",
                "entity_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "detail": None,
            }
        ],
    }
    with patch("app.admin_routes._crm.get_import_batch", return_value=state):
        response = client.get(f"/admin/imports/batches/{batch_id}", cookies=authenticated_admin)
    assert response.status_code == 200
    assert "Rollback batch" in response.text
    assert "Ada" in response.text

    with patch("app.admin_routes._crm.get_import_batch", return_value=None):
        missing = client.get(f"/admin/imports/batches/{batch_id}", cookies=authenticated_admin)
    assert missing.status_code == 404


@pytest.mark.unit
@pytest.mark.integration
def test_import_batch_rollback_redirects(
    authenticated_admin: dict[str, str],
) -> None:
    batch_id = "11111111-1111-1111-1111-111111111111"
    with (
        patch("app.admin_routes._verify_session_csrf"),
        patch("app.admin_routes._crm.rollback_import_batch", return_value={"batch": {"id": batch_id}}),
    ):
        ok = client.post(
            f"/admin/imports/batches/{batch_id}/rollback",
            cookies=authenticated_admin,
            data={"csrf_token": "csrf-ok"},
        )
    assert ok.status_code == 303
    assert ok.headers["location"] == f"/admin/imports/batches/{batch_id}"

    with (
        patch("app.admin_routes._verify_session_csrf"),
        patch(
            "app.admin_routes._crm.rollback_import_batch",
            side_effect=ValueError("Only committed import batches can be rolled back."),
        ),
    ):
        failed = client.post(
            f"/admin/imports/batches/{batch_id}/rollback",
            cookies=authenticated_admin,
            data={"csrf_token": "csrf-ok"},
        )
    assert failed.status_code == 303
    assert "error=" in failed.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_api_requires_auth() -> None:
    response = client.post(
        "/admin/api/imports/linkedin/commit",
        json={"connections": [{"profile_url": "https://linkedin.com/in/ada", "full_name": "Ada"}]},
    )
    assert response.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_api_persists_batch(
    authenticated_admin: dict[str, str],
) -> None:
    with patch("app.admin_routes._crm.commit_linkedin_import") as commit:
        commit.return_value = {
            "batch": {
                "id": "11111111-1111-1111-1111-111111111111",
                "status": "committed",
                "checksum": "abc123",
            },
            "idempotent": False,
            "summary_counts": {"inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0, "conflicted": 0},
        }
        response = client.post(
            "/admin/api/imports/linkedin/commit",
            cookies=authenticated_admin,
            json={
                "export_date": "2026-01-15",
                "connections": [
                    {
                        "profile_url": "https://linkedin.com/in/ada-lovelace",
                        "full_name": "Ada Lovelace",
                    }
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["summary_counts"]["inserted"] == 1
    commit.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_linkedin_commit_api_rejects_bad_payload(
    authenticated_admin: dict[str, str],
) -> None:
    response = client.post(
        "/admin/api/imports/linkedin/commit",
        cookies=authenticated_admin,
        json={"connections": "not-a-list"},
    )
    assert response.status_code == 400
