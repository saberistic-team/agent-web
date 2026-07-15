"""Integration tests for research record persistence and admin routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import admin_auth
from app.crm_service import CrmRepositories, CrmService
from app.main import app
from app.repositories.postgres import PostgresResearchRecordRepository
from app.research_records import ResearchRecordCreate, is_stale, validate_source_url

client = TestClient(app, follow_redirects=False)

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CSRF_TOKEN = "csrf-integration"


def _fake_session() -> admin_auth.AdminSession:
    return admin_auth.AdminSession(
        id=1,
        admin_username=TEST_USERNAME,
        token_hash="session-hash",
        csrf_token_hash=admin_auth.hash_csrf_token(CSRF_TOKEN),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    admin_auth.reset_login_rate_limiter()


@pytest.mark.integration
def test_research_record_repository_round_trip_with_mock_conn() -> None:
    repo = PostgresResearchRecordRepository()
    row = {
        "id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        "record_type": "relationship_context",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "body": "Met at conference",
        "source_name": None,
        "source_url": None,
        "observed_value": None,
        "observed_at": None,
        "confidence": None,
        "review_at": None,
        "expires_at": None,
    }
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = row
    cur.fetchall.return_value = [row]

    created = repo.create(
        conn,
        record_type="relationship_context",
        company_id=COMPANY_ID,
        body="Met at conference",
        contact_id=CONTACT_ID,
    )
    assert created["body"] == "Met at conference"
    assert len(repo.list_for_company(conn, COMPANY_ID)) == 1
    assert len(repo.list_for_contact(conn, CONTACT_ID)) == 1


@pytest.mark.integration
def test_crm_service_attach_research_record_commits() -> None:
    research_repo = MagicMock()
    research_repo.create.return_value = {
        "record_type": "outreach_angle",
        "body": "Lead with platform migration",
    }
    service = CrmService(
        repos=CrmRepositories(
            companies=MagicMock(),
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            research_records=research_repo,
            admin_users=MagicMock(),
            stage_history=MagicMock(),
        )
    )
    conn = MagicMock()
    record = service.attach_research_record(
        conn,
        record_type="outreach_angle",
        company_id=COMPANY_ID,
        body="Lead with platform migration",
    )
    assert record["record_type"] == "outreach_angle"
    conn.commit.assert_called_once()


@pytest.mark.integration
def test_research_validation_and_staleness_for_public_evidence() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    payload = ResearchRecordCreate(
        record_type="verified_fact",
        body="Revenue grew",
        source_name="Annual report",
        source_url="https://reports.example.com/2025",
        observed_value="$12M ARR",
        observed_at=now.isoformat(),
        confidence=0.88,
        review_at=(now + timedelta(days=30)).isoformat(),
        expires_at=(now + timedelta(days=60)).isoformat(),
    )
    assert validate_source_url(payload.source_url or "") == payload.source_url
    stale_record = {
        "record_type": "verified_fact",
        "expires_at": now - timedelta(days=1),
    }
    assert is_stale(stale_record, now=now) is True


@pytest.mark.integration
def test_admin_research_routes_render_and_attach() -> None:
    records: list[dict[str, Any]] = []
    crm = MagicMock()
    crm.list_companies.return_value = [
        {"id": COMPANY_ID, "name": "Acme", "status": "prospect"}
    ]
    crm.get_company.return_value = {"id": COMPANY_ID, "name": "Acme", "status": "prospect"}
    crm.list_contacts_for_company.return_value = [
        {"id": CONTACT_ID, "email": "lead@acme.dev", "company_id": COMPANY_ID}
    ]
    crm.get_contact.return_value = {
        "id": CONTACT_ID,
        "email": "lead@acme.dev",
        "company_id": COMPANY_ID,
    }
    crm.list_research_for_company.side_effect = lambda *args, **kwargs: list(records)
    crm.list_research_for_contact.side_effect = lambda *args, **kwargs: list(records)

    def attach(conn: Any, **kwargs: Any) -> dict[str, Any]:
        record = {"record_type": kwargs["record_type"], "body": kwargs["body"], **kwargs}
        records.append(record)
        return record

    crm.attach_research_record.side_effect = attach

    with (
        patch("app.admin_routes._crm", crm),
        patch("app.admin_routes.db.db_connection") as db_conn,
        patch("app.admin_routes._session_csrf_for_forms", return_value=CSRF_TOKEN),
        patch(
            "app.admin_auth.verify_session_csrf_request",
            side_effect=lambda _request, submitted, _settings: submitted == CSRF_TOKEN,
        ),
        patch("app.admin_routes.require_admin_session", return_value=_fake_session()),
    ):
        db_conn.return_value.__enter__.return_value = MagicMock()
        companies = client.get("/admin/companies")
        company_page = client.get(f"/admin/companies/{COMPANY_ID}")
        create = client.post(
            f"/admin/companies/{COMPANY_ID}/research",
            data={
                "csrf_token": CSRF_TOKEN,
                "record_type": "hypothesis",
                "body": "Evaluating vendors",
            },
        )
        contact_page = client.get(f"/admin/contacts/{CONTACT_ID}")

    assert companies.status_code == 200
    assert company_page.status_code == 200
    assert create.status_code == 303
    assert contact_page.status_code == 200
    assert len(records) == 1
