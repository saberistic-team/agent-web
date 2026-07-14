"""Integration tests for CRM storage boundaries (mocked Postgres)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app import db
from app.crm_service import CrmService
from app.migrations.runner import apply_migrations


COMPANY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTACT_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


@contextmanager
def mock_db_connection():
    conn = MagicMock()
    with patch("app.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.integration
def test_init_db_applies_versioned_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    conn = MagicMock()
    with patch("app.db.psycopg.connect") as connect:
        connect.return_value.__enter__.return_value = conn
        connect.return_value.__exit__.return_value = None
        with patch("app.db.apply_migrations") as apply:
            db.init_db("postgresql://test:test@localhost:5432/test")
            apply.assert_called_once_with(conn)


@pytest.mark.integration
def test_brief_routes_still_use_db_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")

    client = TestClient(app)
    with mock_db_connection() as conn:
        with patch("app.main.db.create_brief", return_value=12) as create_brief:
            with patch(
                "app.main.stripe_service.create_checkout_session",
                return_value=MagicMock(url="https://checkout.stripe.com/x", id="cs_x"),
            ):
                response = client.post(
                    "/api/briefs",
                    json={
                        "website": "https://example.com",
                        "email": "lead@example.com",
                        "brief": "Need architecture help with our platform.",
                    },
                )
    assert response.status_code == 200
    create_brief.assert_called_once()
    assert create_brief.call_args.args[0] is conn


@pytest.mark.integration
def test_crm_service_does_not_touch_brief_tables() -> None:
    service = CrmService()
    conn = MagicMock()
    with patch.object(service._repos.companies, "create", return_value={"id": COMPANY_ID}) as create_company:
        with patch.object(
            service._repos.contacts,
            "create",
            return_value={"id": CONTACT_ID},
        ) as create_contact:
            service.record_company_with_contact(
                conn,
                company_name="Acme",
                website="https://acme.dev",
                contact_email="lead@example.com",
            )
    create_company.assert_called_once()
    create_contact.assert_called_once()
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()
    for call in (create_company, create_contact):
        sql = str(call.call_args)
        assert "project_briefs" not in sql


@pytest.mark.integration
def test_apply_migrations_records_schema_versions() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []

    versions = apply_migrations(conn)
    assert versions == ["001", "002", "003", "004", "005", "006"]
    insert_calls = [
        call
        for call in cur.execute.call_args_list
        if "schema_migrations" in str(call.args[0]) and "INSERT" in str(call.args[0])
    ]
    assert len(insert_calls) == 6
