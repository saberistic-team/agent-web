"""Unit coverage for contact validation, duplicate warnings, and persistence."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.admin_contacts import render_contact_form_page, render_contacts_list_page
from app.contacts import (
    ContactCreate,
    find_contact_duplicate_warnings,
    normalize_email,
    normalize_profile_url,
)
from app.repositories.postgres import PostgresContactRepository

CONTACT_ID = UUID("11111111-1111-1111-1111-111111111111")
COMPANY_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_CONTACT_ID = UUID("33333333-3333-3333-3333-333333333333")


def _conn(rows: list[dict] | None = None, *, fetchone: dict | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    cursor.fetchone.return_value = fetchone
    return conn


@pytest.mark.unit
def test_profile_url_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Alice/ ") == "https://linkedin.com/in/alice"
    assert normalize_email("  Lead@Example.COM ") == "lead@example.com"
    contact = ContactCreate(
        full_name=" Alice  Doe ",
        profile_url="linkedin.com/in/alice",
        email="Lead@Example.com",
        buying_roles=["founder", "technical_buyer"],
    )
    assert contact.full_name == "Alice Doe"
    assert contact.profile_url == "https://linkedin.com/in/alice"
    assert contact.email == "lead@example.com"
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_unknown_registry_values_and_multiple_roles_are_validated() -> None:
    contact = ContactCreate(full_name="Alex", relationship_strength="", buying_roles=[])
    assert contact.relationship_strength is None
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(full_name="Alex", buying_roles=["ceo"])
    with pytest.raises(ValidationError, match="unknown relationship strength"):
        ContactCreate(full_name="Alex", relationship_strength="best-friends")


@pytest.mark.unit
def test_duplicate_warnings_ignore_archived_and_self() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Alex Doe",
            "email": "alex@acme.dev",
            "profile_url": "https://linkedin.com/in/alex",
            "company_id": COMPANY_ID,
        },
        {
            "id": OTHER_CONTACT_ID,
            "full_name": "Alex Doe",
            "email": "alex@acme.dev",
            "profile_url": "https://linkedin.com/in/alex",
            "company_id": COMPANY_ID,
            "archived_at": date.today(),
        },
    ]
    assert find_contact_duplicate_warnings(
        contacts,
        profile_url="https://www.linkedin.com/in/alex",
        email="ALEX@acme.dev",
        full_name="Alex Doe",
        company_id=COMPANY_ID,
        exclude_contact_id=CONTACT_ID,
    ) == []
    warnings = find_contact_duplicate_warnings(
        contacts,
        profile_url="https://linkedin.com/in/alex",
        email="alex@acme.dev",
        full_name="Alex Doe",
        company_id=COMPANY_ID,
    )
    reasons = {warning.reason for warning in warnings}
    assert reasons == {"profile_url", "email", "name_company"}


@pytest.mark.unit
def test_contact_repository_search_archive_and_role_queries() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(conn, query="alex", company_id=COMPANY_ID, include_archived=False)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql and "archived_at IS NULL" in sql and "company_id = %s" in sql

    archived = _conn(fetchone={"id": CONTACT_ID, "full_name": "Alex"})
    repo.archive(archived, CONTACT_ID)
    archive_calls = [
        str(call.args[0])
        for call in archived.cursor.return_value.__enter__.return_value.execute.call_args_list
    ]
    assert any("UPDATE contacts SET archived_at" in sql for sql in archive_calls)
    assert all("DELETE" not in sql for sql in archive_calls)

    roles_conn = _conn(rows=[{"contact_id": CONTACT_ID, "role": "founder"}])
    roles = repo.get_buying_roles(roles_conn, CONTACT_ID)
    assert roles == ["founder"]


@pytest.mark.unit
def test_contact_admin_pages_render_filters_forms_and_warnings() -> None:
    company = {"id": COMPANY_ID, "name": "Acme"}
    contact = {
        "id": CONTACT_ID,
        "full_name": "Alex Doe",
        "title": "CTO",
        "company_id": COMPANY_ID,
        "email": "alex@acme.dev",
        "buying_roles": ["founder"],
        "last_interaction_at": date(2026, 7, 14),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        companies_by_id={str(COMPANY_ID): company},
        filters={"q": "Alex", "company_id": str(COMPANY_ID), "archived": None},
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add contact" in listing and "Founder" in listing and "Acme" in listing
    edit = render_contact_form_page(
        contact=contact,
        companies=[company],
        csrf_token="csrf",
        admin_username="admin",
        error_message="warning",
    )
    assert "/edit" in edit and "warning" in edit and "Technical buyer" in edit
