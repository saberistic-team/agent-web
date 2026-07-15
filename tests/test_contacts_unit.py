"""Unit coverage for contact validation, duplicate warnings, and persistence filters."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.admin_contacts import render_contact_form_page, render_contacts_list_page
from app.contacts import (
    BUYING_ROLES,
    ContactCreate,
    find_contact_duplicate_warnings,
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
def test_profile_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Alice/ ") == "linkedin.com/in/alice"
    assert normalize_email(" Lead@Acme.COM ") == "lead@acme.com"
    contact = ContactCreate(
        full_name=" Alice Example ",
        profile_url="linkedin.com/in/alice",
        email="lead@acme.com",
        email_permission="permitted",
        buying_roles=["founder", "technical_buyer"],
    )
    assert contact.full_name == "Alice Example"
    assert contact.profile_url == "linkedin.com/in/alice"
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_unknown_buying_roles_and_email_permission_requirements() -> None:
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(full_name="Alex", buying_roles=["ceo"])
    with pytest.raises(ValidationError, match="email permission requires"):
        ContactCreate(full_name="Alex", email_permission="permitted")


@pytest.mark.unit
def test_duplicate_warnings_cover_profile_email_and_name_company() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Alice Example",
            "email": "alice@acme.com",
            "profile_url_normalized": "linkedin.com/in/alice",
            "company_id": COMPANY_ID,
        },
        {
            "id": UUID("33333333-3333-3333-3333-333333333333"),
            "full_name": "Archived",
            "email": "archived@acme.com",
            "profile_url_normalized": "linkedin.com/in/archived",
            "company_id": COMPANY_ID,
            "archived_at": date(2026, 7, 1),
        },
    ]
    warnings = find_contact_duplicate_warnings(
        contacts,
        profile_url="https://www.linkedin.com/in/alice",
        email="alice@acme.com",
        full_name="Alice Example",
        company_id=COMPANY_ID,
        exclude_contact_id=CONTACT_ID,
    )
    assert warnings == []
    warnings = find_contact_duplicate_warnings(
        contacts,
        profile_url="https://linkedin.com/in/alice",
        email="alice@acme.com",
        full_name="Alice Example",
        company_id=COMPANY_ID,
    )
    reasons = {warning.reason for warning in warnings}
    assert reasons == {"profile URL", "email", "name and company"}


@pytest.mark.unit
def test_contact_repository_search_archive_and_role_filter() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(conn, query="alice", buying_role="founder", include_archived=False)
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql
    assert "buying_roles @>" in sql
    assert "archived_at IS NULL" in sql

    archived = _conn()
    repo.archive(archived, CONTACT_ID)
    archive_sql = str(archived.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE contacts SET archived_at" in archive_sql
    assert "DELETE" not in archive_sql


@pytest.mark.unit
def test_contact_admin_pages_render_roles_filters_and_warnings() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Alice Example",
        "title": "CTO",
        "company_name": "Acme",
        "email": "alice@acme.com",
        "buying_roles": ["founder", "technical_buyer"],
        "last_interaction_at": date(2026, 7, 14),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        filters={"q": "Alice", "buying_role": "founder", "archived": None},
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add contact" in listing
    assert "Founder" in listing and "Technical buyer" in listing
    edit = render_contact_form_page(
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contact=contact,
        csrf_token="csrf",
        admin_username="admin",
        warnings=[
            type("Warning", (), {"contact_id": str(CONTACT_ID), "full_name": "Other", "reason": "email"})()
        ],
    )
    assert "/edit" in edit
    assert "email" in edit
    for role in BUYING_ROLES:
        assert role in edit
