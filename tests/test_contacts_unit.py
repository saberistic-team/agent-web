"""Unit coverage for contact validation, duplicate warnings, and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.admin_contacts import render_contact_form_page, render_contacts_list_page
from app.contacts import (
    ContactCreate,
    collect_contact_duplicate_warnings,
    normalize_email,
    normalize_profile_url,
)
from app.repositories.postgres import PostgresContactRepository


COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    return conn


@pytest.mark.unit
def test_profile_url_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Acme/ ") == (
        "https://linkedin.com/in/acme"
    )
    assert normalize_email(" Lead@Example.COM ") == "lead@example.com"
    contact = ContactCreate(
        full_name="Alex Kim",
        company_id=COMPANY_ID,
        profile_url="linkedin.com/in/alex",
        email="Alex@Example.com",
        buying_roles=["founder", "technical_buyer"],
    )
    assert contact.profile_url == "https://linkedin.com/in/alex"
    assert contact.email == "alex@example.com"
    assert contact.email_permission == "unverified"
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_contact_validation_rejects_unknown_roles_and_allows_optional_email() -> None:
    contact = ContactCreate(
        full_name="Alex Kim",
        company_id=COMPANY_ID,
        buying_roles=[],
    )
    assert contact.email is None and contact.email_permission is None
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(
            full_name="Alex Kim",
            company_id=COMPANY_ID,
            buying_roles=["champion"],
        )


@pytest.mark.unit
def test_duplicate_warnings_cover_profile_email_and_name_company() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Alex Kim",
            "company_id": COMPANY_ID,
            "profile_url": "https://linkedin.com/in/alex",
            "email": "alex@example.com",
        },
        {
            "id": UUID("33333333-3333-3333-3333-333333333333"),
            "full_name": "Alex Kim",
            "company_id": COMPANY_ID,
            "archived_at": datetime.now(timezone.utc),
        },
    ]
    warnings = collect_contact_duplicate_warnings(
        contacts,
        full_name="Alex Kim",
        company_id=COMPANY_ID,
        profile_url="https://www.linkedin.com/in/alex",
        email="ALEX@example.com",
    )
    reasons = {warning.reason for warning in warnings}
    assert reasons == {"profile URL", "email", "name at company"}


@pytest.mark.unit
def test_contact_repository_search_archive_and_role_filters() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(conn, query="alex", buying_role="founder")
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql
    assert "contact_buying_roles" in sql
    assert "archived_at IS NULL" in sql

    archived = _conn()
    cur = archived.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {
        "id": CONTACT_ID,
        "email": "lead@example.com",
        "full_name": "Lead",
        "company_id": COMPANY_ID,
    }
    cur.fetchall.return_value = []
    repo.archive(archived, CONTACT_ID)
    execute_calls = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert any("UPDATE contacts SET archived_at" in sql for sql in execute_calls)
    assert all("DELETE FROM contacts" not in sql for sql in execute_calls)


@pytest.mark.unit
def test_contact_admin_pages_render_filters_forms_and_warnings() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Alex Kim",
        "title": "CTO",
        "company_id": COMPANY_ID,
        "company_name": "Acme",
        "email": "alex@acme.dev",
        "buying_roles": ["technical_buyer"],
        "last_interaction_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        filters={"q": "Alex", "buying_role": "technical_buyer", "archived": None},
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add contact" in listing and "Technical buyer" in listing
    edit = render_contact_form_page(
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
        csrf_token="csrf",
        admin_username="admin",
        error_message="warning",
    )
    assert "Save contact" in edit and "warning" in edit and "Technical buyer" in edit
