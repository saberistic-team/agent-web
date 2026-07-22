"""End-to-end acquisition lifecycle against live PostgreSQL (#130).

Exercises the core operator path: login → CRM entities → evidence → import
commit/replay → discovery candidate review → scoring → pipeline action →
analytics event → export request.

Runs in the isolated PostgreSQL contract workflow (marker: ``contract``), not the
fast unit/integration job. Uses deterministic fixtures only — no live discovery
network calls.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import admin_auth, analytics_ingest, db
from app.acquisition_pipeline import PipelineStageChange
from app.actor_context import ActorContext
from app.companies import CompanyCreate
from app.config import get_settings
from app.contacts import ContactCreate
from app.crm_service import CrmService
from app.discovery.adapters import build_api_adapter
from app.discovery.category import crm_category_for_discovery, map_suggested_category
from app.discovery.fetcher import HttpFetcher
from app.discovery.runner import run_adapter
from app.discovery.types import DiscoveryCheckpoint
from app.linkedin_import import compute_import_checksum
from app.main import app
from tests.test_admin_auth import (
    TEST_PASSWORD,
    TEST_USERNAME,
    _extract_csrf_token,
    _extract_session_cookie,
    _parse_login_form,
)

TEST_HASH = PasswordHasher().hash(TEST_PASSWORD)
TEST_SECRET = "test-session-secret-32chars-minimum"
TEST_LIMITER_SECRET = "test-limiter-secret-32chars-minimum!!"
ACTOR = ActorContext(actor=TEST_USERNAME, correlation_id="e2e-lifecycle-1")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery"
IDEMPOTENCY_KEY = "660e8400-e29b-41d4-a716-446655440001"
ANALYTICS_SESSION = "550e8400-e29b-41d4-a716-446655440000"

_ADMITTED = db.AdminLoginAdmission(
    admitted=True,
    throttled=False,
    already_locked=False,
    lockout_transition=False,
)

client = TestClient(app, follow_redirects=False)


def _fixture_loader(url: str) -> bytes:
    if "api.example.com" in url:
        return (FIXTURES / "sample-api.json").read_bytes()
    raise FileNotFoundError(url)


@pytest.fixture
def lifecycle_env(
    migrated_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    database_url = str(migrated_conn.info.dsn)
    if "dbname=" not in database_url:
        database_url = migrated_conn.info.get_parameters().get("dbname", "")
    # migrated_conn fixture does not expose URL; use TEST_DATABASE_URL from env.
    import os

    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    assert url, "TEST_DATABASE_URL required for lifecycle e2e"

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", TEST_LIMITER_SECRET)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    admin_auth.reset_login_rate_limiter()
    yield {"conn": migrated_conn, "url": url, "crm": CrmService()}


def _admin_login() -> dict[str, str]:
    with patch("app.admin_auth.try_admit_login_attempt", return_value=_ADMITTED):
        form = client.get("/admin/login")
        assert form.status_code == 200
        csrf, cookies = _parse_login_form(form)
        response = client.post(
            "/admin/login",
            data={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
                "csrf_token": csrf,
            },
            cookies=cookies,
        )
    assert response.status_code == 303
    session = _extract_session_cookie(response)
    assert session is not None
    return {admin_auth.SESSION_COOKIE_NAME: session}


def _session_csrf(cookies: dict[str, str]) -> str:
    dashboard = client.get("/admin", cookies=cookies)
    assert dashboard.status_code == 200
    return _extract_csrf_token(dashboard.text)


@pytest.mark.contract
def test_acquisition_lifecycle_end_to_end(lifecycle_env: dict[str, Any]) -> None:
    conn: psycopg.Connection = lifecycle_env["conn"]
    crm: CrmService = lifecycle_env["crm"]

    # 1. Admin login
    session_cookies = _admin_login()

    # 2. Company + contact creation
    created = crm.create_company(
        conn,
        company=CompanyCreate(
            name="Lifecycle Labs",
            website="https://lifecycle.example",
            domain="lifecycle.example",
            category="ai_infrastructure",
        ),
        actor_context=ACTOR,
    )
    company = created["company"]
    company_id = UUID(str(company["id"]))
    conn.commit()

    contact_result = crm.create_contact(
        conn,
        contact=ContactCreate(
            full_name="Alex Operator",
            email="alex@lifecycle.example",
            company_id=company_id,
            title="CTO",
        ),
        actor_context=ACTOR,
    )
    contact = contact_result["contact"]
    contact_id = UUID(str(contact["id"]))
    conn.commit()

    # 3. Evidence (research record)
    record = crm.attach_research_record(
        conn,
        actor_context=ACTOR,
        record_type="public_signal",
        company_id=company_id,
        contact_id=contact_id,
        body="Published platform migration case study",
        source_url="https://lifecycle.example/signals/1",
        observed_at=datetime.now(timezone.utc),
        confidence=0.85,
    )
    conn.commit()
    assert record["record_type"] == "public_signal"

    # 4. Import preview (checksum) → commit → replay
    connections = [
        {
            "profile_url": "https://linkedin.com/in/lifecycle-import",
            "full_name": "Import Contact",
            "title": "Engineer",
            "company": "Lifecycle Labs",
        }
    ]
    checksum = compute_import_checksum(connections)
    assert checksum  # preview-derived checksum is stable before commit

    first_import = crm.commit_linkedin_import(
        conn,
        actor_context=ACTOR,
        connections=connections,
        checksum=checksum,
    )
    conn.commit()
    assert first_import["idempotent"] is False
    batch_id = UUID(str(first_import["batch"]["id"]))

    replay = crm.commit_linkedin_import(
        conn,
        actor_context=ACTOR,
        connections=connections,
        checksum=checksum,
    )
    conn.commit()
    assert replay["idempotent"] is True
    assert UUID(str(replay["batch"]["id"])) == batch_id

    # HTTP commit path (authenticated)
    csrf = _session_csrf(session_cookies)
    api_response = client.post(
        "/admin/api/imports/linkedin/commit",
        cookies=session_cookies,
        headers={
            admin_auth.CSRF_HEADER_NAME: csrf,
            "Content-Type": "application/json",
        },
        json={"connections": connections, "checksum": checksum},
    )
    assert api_response.status_code == 200
    assert api_response.json()["idempotent"] is True

    # 5. Discovery candidate review (fixture adapter → operator promote)
    adapter = build_api_adapter(
        source_id="fixture_api",
        documented=True,
        api_url="https://api.example.com/companies.json",
    )
    fetcher = HttpFetcher(fixture_loader=_fixture_loader)
    discovery = run_adapter(adapter, checkpoint=DiscoveryCheckpoint(cursor="0"), fetcher=fetcher)
    assert discovery.candidates
    candidate = next(c for c in discovery.candidates if c.name == "Nimbus Analytics")
    suggested = map_suggested_category(tags=list(candidate.signals), description=None)
    category = crm_category_for_discovery(suggested)

    promoted = crm.create_company(
        conn,
        company=CompanyCreate(
            name=candidate.name,
            website=candidate.website,
            domain=candidate.domain,
            category=category,
            notes="Promoted from discovery candidate review",
        ),
        actor_context=ACTOR,
    )
    conn.commit()
    assert promoted["company"]["name"] == "Nimbus Analytics"

    # 6. Scoring rule update
    scoring = crm.update_scoring_rule(
        conn,
        actor_context=ACTOR,
        rule_id="intent-weight-v1",
        summary_before={"weight": 1.0},
        summary_after={"weight": 1.2, "signal": "hiring"},
    )
    conn.commit()
    assert scoring["rule_id"] == "intent-weight-v1"

    # 7. Pipeline action
    crm.assign_company_to_pipeline(
        conn,
        actor_context=ACTOR,
        company_id=company_id,
        initial_stage="researching",
    )
    conn.commit()
    transitioned = crm.transition_pipeline_stage(
        conn,
        actor_context=ACTOR,
        company_id=company_id,
        change=PipelineStageChange(to_stage="qualified"),
    )
    conn.commit()
    assert transitioned["company"]["pipeline_stage"] == "qualified"

    # 8. Analytics event (first-party ingest)
    settings = get_settings()
    event_body = {
        "idempotency_key": IDEMPOTENCY_KEY,
        "event_name": "Landing Viewed",
        "schema_version": "1.0.0",
        "occurred_at": "2026-07-15T12:00:00+00:00",
        "anonymous_session_id": ANALYTICS_SESSION,
        "path_class": "landing",
        "referrer_class": "direct",
        "properties": {"page": "/", "funnel_step": 1},
        "consent_state": "implicit_analytics",
    }
    ingest_result = analytics_ingest.ingest_browser_event(
        settings,
        raw_body=json.dumps(event_body).encode(),
        origin="http://testserver",
        referer=None,
        dnt_header=None,
        user_agent="Mozilla/5.0",
        source_key="127.0.0.1",
        conn=conn,
    )
    conn.commit()
    assert ingest_result.accepted
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM analytics_events WHERE idempotency_key = %s",
            (IDEMPOTENCY_KEY,),
        )
        row = cur.fetchone()
    assert row is not None and int(row["n"]) == 1

    # 9. Export request
    export = crm.request_export(
        conn,
        actor_context=ACTOR,
        export_type="crm_contacts",
        filters={"company_id": str(company_id)},
    )
    conn.commit()
    assert export["export_type"] == "crm_contacts"

    # Audit trail contains key lifecycle actions
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT action FROM audit_events
            WHERE action IN (
                'auth.login.success', 'import.batch', 'scoring_rule.update',
                'pipeline.update', 'export.request'
            )
            ORDER BY created_at ASC
            """
        )
        actions = {str(row["action"]) for row in cur.fetchall()}
    assert "auth.login.success" in actions
    assert "import.batch" in actions
    assert "scoring_rule.update" in actions
    assert "pipeline.update" in actions
    assert "export.request" in actions

    # Admin HTML surfaces respond for authenticated operator
    for path in ("/admin/companies", "/admin/contacts", "/admin/imports/batches"):
        page = client.get(path, cookies=session_cookies)
        assert page.status_code == 200
        assert "admin-app" in page.text or "admin-nav" in page.text

    # Import preview page reachable
    imports_page = client.get("/admin/imports", cookies=session_cookies)
    assert imports_page.status_code == 200
    assert re.search(r"import|LinkedIn", imports_page.text, re.I)
