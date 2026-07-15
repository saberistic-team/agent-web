"""Unit coverage for contact validation, duplicate warnings, and persistence filters."""

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
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
    normalize_email,
    normalize_profile_url,
)
from app.repositories.postgres import PostgresContactRepository


CONTACT_ID = UUID("11111111-1111-1111-1111-111111111111")
COMPANY_ID = UUID("22222222-2222-2222-2222-222222222222")


def _conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    return conn


@pytest.mark.unit
def test_profile_url_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Ada/ ") == "https://linkedin.com/in/Ada"
    assert normalize_email(" Lead@Example.COM ") == "lead@example.com"
    contact = ContactCreate(
        full_name=" Ada Lovelace ",
        profile_url="linkedin.com/in/ada",
        email="lead@example.com",
        buying_roles=["founder", "technical_buyer", "founder"],
    )
    assert contact.full_name == "Ada Lovelace"
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_unknown_registry_values_and_optional_email_rules() -> None:
    contact = ContactCreate(full_name="Ada", relationship_strength="", buying_roles=[])
    assert contact.relationship_strength is None
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(full_name="Ada", buying_roles=["champion"])
    with pytest.raises(ValidationError, match="email provenance requires"):
        ContactCreate(full_name="Ada", email_source="manual")


@pytest.mark.unit
def test_duplicate_warnings_ignore_archived_and_self() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "https://linkedin.com/in/ada",
            "company_id": COMPANY_ID,
        },
        {
            "id": UUID("33333333-3333-3333-3333-333333333333"),
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "https://linkedin.com/in/ada",
            "company_id": COMPANY_ID,
            "archived_at": datetime.now(timezone.utc),
        },
    ]
    assert find_profile_url_duplicate_warnings(
        contacts, profile_url="https://www.linkedin.com/in/ada", exclude_contact_id=CONTACT_ID
    ) == []
    warnings = collect_contact_duplicate_warnings(
        contacts,
        full_name="Ada Lovelace",
        profile_url="https://linkedin.com/in/ada",
        email="ADA@example.com",
        company_id=COMPANY_ID,
    )
    assert len(warnings) == 1 and warnings[0].reason == "profile URL"
    assert find_email_duplicate_warnings(contacts, email="ada@example.com")[0].reason == "email"
    assert (
        find_name_company_duplicate_warnings(
            contacts, full_name="Ada Lovelace", company_id=COMPANY_ID
        )[0].reason
        == "name and company"
    )


@pytest.mark.unit
def test_contact_repository_search_filters_and_archive_are_non_destructive() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(
        conn,
        query="ada",
        buying_role="founder",
        relationship_strength="warm",
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql and "%s = ANY(c.buying_roles)" in sql
    assert "archived_at IS NULL" in sql

    archived = _conn()
    repo.archive(archived, CONTACT_ID)
    archive_sql = str(archived.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE contacts SET archived_at" in archive_sql
    assert "DELETE" not in archive_sql


@pytest.mark.unit
def test_contact_admin_pages_render_filters_forms_and_warnings() -> None:
    contact = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "CTO",
        "company_name": "Acme",
        "email": "ada@example.com",
        "buying_roles": ["technical_buyer"],
        "last_interaction_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        filters={"q": "Ada", "buying_role": "technical_buyer", "relationship_strength": None, "archived": None},
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add contact" in listing and "Technical buyer" in listing
    edit = render_contact_form_page(
        contact=contact,
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        csrf_token="csrf",
        admin_username="admin",
        error_message="warning",
    )
    assert "/edit" in edit and "warning" in edit and "Technical buyer" in edit
