"""Unit tests for lead discovery inbox admin routes."""

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
from app.main import app
from tests.conftest import enable_admin_preview_env

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_HASH = PasswordHasher().hash("correct-horse-battery-staple")
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
CANDIDATE_ID = UUID("11111111-1111-1111-1111-111111111101")

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
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_raw = admin_auth.generate_csrf_value()
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
        patch("app.admin_discovery_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn
        cookies = {SESSION_COOKIE_NAME: raw_token}
        response = client.get("/admin/discovery/inbox", cookies=cookies)
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert match is not None
        yield {"cookies": cookies, "csrf_token": match.group(1), "conn": mock_conn}


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_inbox_requires_auth() -> None:
    response = client.get("/admin/discovery/inbox")
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_inbox_renders_for_authenticated_user(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    inbox.list_candidates.return_value = [
        {
            "id": str(CANDIDATE_ID),
            "name": "Northwind Labs",
            "source_id": "fixture_api",
            "category": "fintech",
            "confidence": 0.82,
            "freshness": "fresh",
            "review_state": "pending",
            "discovered_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
        }
    ]
    inbox.list_filter_metadata.return_value = {"sources": ["fixture_api"], "runs": []}
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.get("/admin/discovery/inbox", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200
    assert "Review inbox" in response.text
    assert "Northwind Labs" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_candidate_detail_renders(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    inbox.get_candidate_detail.return_value = {
        "id": CANDIDATE_ID,
        "name": "Northwind Labs",
        "source_id": "fixture_api",
        "review_state": "pending",
        "domain": "northwind.example",
        "website": "https://northwind.example",
        "category": "fintech",
        "confidence": 0.82,
        "freshness": "fresh",
        "discovered_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
        "signals": ["hiring"],
        "evidence": {"snippet": "Seed round", "observations": []},
        "conflicts": [],
        "match_suggestions": [],
    }
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.get(
            f"/admin/discovery/inbox/{CANDIDATE_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Supporting evidence" in response.text
    assert "Northwind Labs" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_accept_requires_csrf(authenticated_admin: dict[str, Any]) -> None:
    response = client.post(
        f"/admin/discovery/inbox/{CANDIDATE_ID}/accept",
        data={"csrf_token": "bad", "company_choice": "new"},
        cookies=authenticated_admin["cookies"],
    )
    assert response.status_code == 403


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_accept_redirects_to_company(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    company_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    inbox.accept_candidate.return_value = {"company": {"id": company_id}}
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/accept",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "company_choice": "new",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert f"/admin/companies/{company_id}" in response.headers["location"]


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_reject_redirects_to_inbox(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/reject",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "rejection_reason": "Not a fit",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/discovery/inbox"
    inbox.reject_candidate.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_defer_redirects_to_inbox(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/defer",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "deferred_until": "2026-08-01T12:00",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    inbox.defer_candidate.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_bulk_preview_renders(authenticated_admin: dict[str, Any]) -> None:
    inbox = MagicMock()
    inbox.preview_bulk_action.return_value = {
        "action": "reject",
        "count": 1,
        "candidates": [
            {
                "id": str(CANDIDATE_ID),
                "name": "Northwind Labs",
                "source_id": "fixture_api",
                "domain": "northwind.example",
                "review_state": "pending",
            }
        ],
        "invalid_state_ids": [],
        "preview_token": "token",
    }
    with patch("app.admin_discovery_routes._inbox", inbox):
        response = client.post(
            "/admin/discovery/inbox/bulk/preview",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "action": "reject",
                "candidate_ids": [str(CANDIDATE_ID)],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Bulk action preview" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_inbox_preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get("/admin/discovery/inbox")
    assert response.status_code == 200
    assert "Preview data — not production" in response.text
    assert "Review inbox" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_candidate_preview_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    response = client.get(f"/admin/discovery/inbox/{CANDIDATE_ID}")
    assert response.status_code == 200
    assert "Supporting evidence" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_not_exposed_on_public_site() -> None:
    response = client.get("/discovery")
    assert response.status_code in {404, 405}


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_accept_preview_mode_rejects_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    with patch("app.admin_routes._verify_session_csrf"):
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/accept",
            data={"csrf_token": "ignored", "company_choice": "new"},
        )
    assert response.status_code == 405


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_inbox_db_load_and_filter_validation(authenticated_admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    inbox = MagicMock()
    inbox.list_candidates.return_value = []
    inbox.list_filter_metadata.return_value = {"sources": [], "runs": []}
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.get(
            "/admin/discovery/inbox",
            params={"confidence": "not-a-bucket", "review_state": "pending"},
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Review inbox" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_inbox_db_failure_is_soft(authenticated_admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    inbox = MagicMock()
    inbox.list_candidates.side_effect = RuntimeError("db down")
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.get("/admin/discovery/inbox", cookies=authenticated_admin["cookies"])
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_bulk_preview_limit_error(authenticated_admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from app.discovery_inbox import DiscoveryBulkLimitError

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    inbox = MagicMock()
    inbox.preview_bulk_action.side_effect = DiscoveryBulkLimitError("too many")
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.post(
            "/admin/discovery/inbox/bulk/preview",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "action": "reject",
                "candidate_ids": [str(CANDIDATE_ID)],
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 400
    assert "too many" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_bulk_commit_and_mutations(authenticated_admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    inbox = MagicMock()
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.post(
            "/admin/discovery/inbox/bulk/commit",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "action": "reject",
                "preview_token": "token",
                "candidate_ids": [str(CANDIDATE_ID)],
                "rejection_reason": "Not ICP",
            },
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/discovery/inbox?saved=1"
    inbox.commit_bulk_action.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_candidate_detail_db(authenticated_admin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:5432/agent_web_test")
    inbox = MagicMock()
    inbox.get_candidate_detail.return_value = {
        "id": CANDIDATE_ID,
        "name": "Northwind Labs",
        "source_id": "fixture_api",
        "external_id": "fixture:1",
        "evidence_fingerprint": "fp",
        "domain": "northwind.example",
        "website": "https://northwind.example",
        "category": "fintech",
        "confidence": 0.9,
        "freshness": "fresh",
        "review_state": "pending",
        "discovered_at": datetime.now(timezone.utc),
        "signals": [],
        "evidence": {"snippet": "x", "observations": []},
        "conflicts": [],
        "match_suggestions": [],
    }
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        response = client.get(
            f"/admin/discovery/inbox/{CANDIDATE_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert response.status_code == 200
    assert "Northwind Labs" in response.text

    inbox.get_candidate_detail.return_value = None
    with patch("app.admin_discovery_routes._inbox", inbox), patch(
        "app.admin_discovery_routes.db.db_connection"
    ) as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        missing = client.get(
            f"/admin/discovery/inbox/{CANDIDATE_ID}",
            cookies=authenticated_admin["cookies"],
        )
    assert missing.status_code == 404


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_bulk_preview_get_redirect(authenticated_admin: dict[str, Any]) -> None:
    response = client.get("/admin/discovery/inbox/bulk/preview", cookies=authenticated_admin["cookies"])
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/discovery/inbox"


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_reject_and_defer_preview_mode_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_admin_preview_env(monkeypatch)
    with patch("app.admin_discovery_routes._verify_session_csrf"):
        reject = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/reject",
            data={"csrf_token": "x", "rejection_reason": "nope"},
        )
        defer = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/defer",
            data={"csrf_token": "x", "deferred_until": "2099-01-01T00:00"},
        )
        bulk = client.post(
            "/admin/discovery/inbox/bulk/commit",
            data={
                "csrf_token": "x",
                "action": "reject",
                "preview_token": "t",
                "candidate_ids": [str(CANDIDATE_ID)],
            },
        )
    assert reject.status_code == 405
    assert defer.status_code == 405
    assert bulk.status_code == 405


@pytest.mark.unit
@pytest.mark.integration
def test_discovery_mutations_require_database(authenticated_admin: dict[str, Any]) -> None:
    import dataclasses

    from app.config import get_settings

    settings = dataclasses.replace(get_settings(), database_url="", admin_preview_enabled=False)
    with patch("app.admin_discovery_routes.get_settings", return_value=settings):
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/accept",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "company_choice": "new",
            },
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/reject",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "rejection_reason": "Not ICP",
            },
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503
        response = client.post(
            f"/admin/discovery/inbox/{CANDIDATE_ID}/defer",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "deferred_until": "2099-01-01T00:00",
            },
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503
        response = client.post(
            "/admin/discovery/inbox/bulk/preview",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "action": "reject",
                "candidate_ids": [str(CANDIDATE_ID)],
            },
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503
        response = client.post(
            "/admin/discovery/inbox/bulk/commit",
            data={
                "csrf_token": authenticated_admin["csrf_token"],
                "action": "reject",
                "preview_token": "t",
                "candidate_ids": [str(CANDIDATE_ID)],
            },
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503
        response = client.get(
            f"/admin/discovery/inbox/{CANDIDATE_ID}",
            cookies=authenticated_admin["cookies"],
        )
        assert response.status_code == 503


@pytest.mark.unit
def test_discovery_pages_helper_branches() -> None:
    from app.admin_discovery_pages import (
        _format_timestamp,
        _run_options,
        _source_options,
        render_discovery_bulk_preview_page,
        render_discovery_candidate_page,
        render_discovery_inbox_page,
    )

    assert _format_timestamp(None) == "—"
    assert "2026" in _format_timestamp(datetime(2026, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert _format_timestamp("raw-stamp") == "raw-stamp"
    assert "fixture_api" in _source_options(["fixture_api"], "fixture_api")
    assert "selected" in _run_options(
        [
            {
                "id": "run-1",
                "source_id": "yc",
                "started_at": datetime(2026, 2, 3, tzinfo=timezone.utc),
                "candidate_count": 2,
                "status": "completed",
            }
        ],
        "run-1",
    )
    html = render_discovery_inbox_page(
        candidates=[],
        filters={"review_state": "accepted", "source": "yc", "category": "fintech"},
        filter_metadata={"sources": ["yc"], "runs": []},
        csrf_token="csrf",
        admin_username="ops",
        error_message="boom",
        status_message="saved",
        preview_banner="Preview data — not production",
    )
    assert "saved" in html
    assert "Preview data — not production" in html
    # error_message is only shown when status_message is absent
    html_err = render_discovery_inbox_page(
        candidates=[],
        filters={"review_state": "pending"},
        filter_metadata={"sources": [], "runs": []},
        csrf_token="csrf",
        admin_username="ops",
        error_message="boom",
    )
    assert "boom" in html_err
    detail = render_discovery_candidate_page(
        candidate={
            "id": CANDIDATE_ID,
            "name": "Northwind Labs",
            "source_id": "fixture_api",
            "external_id": "x",
            "evidence_fingerprint": "fp",
            "domain": None,
            "website": None,
            "category": None,
            "confidence": None,
            "freshness": "stale",
            "review_state": "deferred",
            "discovered_at": None,
            "signals": None,
            "evidence": {"snippet": None, "observations": [{"value": "v"}]},
            "conflicts": None,
            "match_suggestions": None,
        },
        csrf_token="csrf",
        admin_username="ops",
        preview_banner="Preview data — not production",
    )
    assert "Northwind Labs" in detail
    preview = render_discovery_bulk_preview_page(
        preview={
            "action": "defer",
            "count": 0,
            "candidates": [],
            "invalid_state_ids": [str(CANDIDATE_ID)],
            "preview_token": "tok",
            "rejection_reason": None,
            "deferred_until": "2099-01-01T00:00:00+00:00",
        },
        csrf_token="csrf",
        admin_username="ops",
        preview_banner="Preview data — not production",
    )
    assert "Bulk action preview" in preview
