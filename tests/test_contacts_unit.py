"""Unit coverage for contact validation, duplicate warnings, and persistence filters."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.admin_contacts import render_contact_form_page, render_contacts_list_page
from app.contacts import (
    ContactCreate,
    DECISION_MAKER_BUYING_ROLES,
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
    normalize_email,
    normalize_profile_url,
)
from app.repositories.postgres import PostgresContactRepository


CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")
COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")


def _conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    return conn


@pytest.mark.unit
def test_profile_url_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Ada-Lovelace/ ") == (
        "https://linkedin.com/in/ada-lovelace"
    )
    assert normalize_email(" Lead@Example.COM ") == "lead@example.com"
    contact = ContactCreate(
        full_name=" Ada Lovelace ",
        profile_url="linkedin.com/in/ada",
        email="",
        buying_roles=["founder", "technical_buyer", "founder"],
    )
    assert contact.full_name == "Ada Lovelace"
    assert contact.email is None
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_decision_maker_buying_roles_policy() -> None:
    assert DECISION_MAKER_BUYING_ROLES == frozenset(
        {"founder", "technical_buyer", "executive_buyer"}
    )
    assert "influencer" not in DECISION_MAKER_BUYING_ROLES
    assert "introducer" not in DECISION_MAKER_BUYING_ROLES
    assert "investor" not in DECISION_MAKER_BUYING_ROLES
    assert "other" not in DECISION_MAKER_BUYING_ROLES


@pytest.mark.unit
def test_unknown_registry_values_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(full_name="Ada", buying_roles=["ceo"])
    with pytest.raises(ValidationError, match="unknown relationship strength"):
        ContactCreate(full_name="Ada", relationship_strength="bestie")
    with pytest.raises(ValidationError, match="unknown CRM context tag"):
        ContactCreate(full_name="Ada", crm_context_tags=["best_friend"])


@pytest.mark.unit
def test_crm_context_tags_accept_known_values() -> None:
    contact = ContactCreate(
        full_name="Ada",
        crm_context_tags=["former_colleague", "warm_introducer", "former_colleague"],
    )
    assert contact.crm_context_tags == ["former_colleague", "warm_introducer"]


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
            "archived_at": date.today(),
        },
    ]
    assert find_profile_url_duplicate_warnings(
        contacts,
        profile_url="https://www.linkedin.com/in/ada",
        exclude_contact_id=CONTACT_ID,
    ) == []
    assert len(find_email_duplicate_warnings(contacts, email="ADA@example.com")) == 1
    assert find_name_company_duplicate_warnings(
        contacts,
        full_name="Ada Lovelace",
        company_id=COMPANY_ID,
        exclude_contact_id=CONTACT_ID,
    ) == []


@pytest.mark.unit
def test_contact_repository_search_filters_and_archive_are_non_destructive() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(
        conn,
        query="ada",
        company_id=COMPANY_ID,
        buying_role="founder",
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql and "company_id = %s" in sql
    assert "ANY(c.buying_roles)" in sql and "archived_at IS NULL" in sql

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
        "buying_roles": ["technical_buyer"],
        "company_id": COMPANY_ID,
        "company_name": "Acme",
        "email": "ada@example.com",
        "last_interaction_at": date(2026, 7, 14),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        filters={"q": "Ada", "company_id": None, "buying_role": None, "archived": None},
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
    assert "/edit" in edit and "warning" in edit
    assert "Technical buyer" in edit
    assert 'value="technical_buyer"' in edit


@pytest.mark.unit
def test_duplicate_warning_helpers_skip_invalid_and_empty_inputs() -> None:
    contacts = [
        {
            "id": UUID("44444444-4444-4444-4444-444444444444"),
            "full_name": "Bad Url",
            "email": "not-an-email",
            "profile_url": "://bad",
            "company_id": COMPANY_ID,
            "archived_at": None,
        },
        {
            "id": UUID("55555555-5555-5555-5555-555555555555"),
            "full_name": "Pat Example",
            "email": "pat@example.com",
            "profile_url": "https://linkedin.com/in/pat",
            "company_id": COMPANY_ID,
            "archived_at": None,
        },
    ]
    assert find_profile_url_duplicate_warnings(contacts, profile_url=None) == []
    assert find_profile_url_duplicate_warnings(contacts, profile_url="https://linkedin.com/in/pat")
    assert find_email_duplicate_warnings(contacts, email=None) == []
    assert find_email_duplicate_warnings(contacts, email="pat@example.com")
    assert find_name_company_duplicate_warnings(
        contacts, full_name="Pat Example", company_id=None
    ) == []
    assert find_name_company_duplicate_warnings(
        contacts, full_name="   ", company_id=COMPANY_ID
    ) == []
    assert find_name_company_duplicate_warnings(
        contacts, full_name="Pat Example", company_id=COMPANY_ID
    )


@pytest.mark.unit
def test_contact_repository_find_helpers_support_exclude_id() -> None:
    repo = PostgresContactRepository()
    conn = _conn([{"id": CONTACT_ID, "full_name": "Ada"}])
    rows = repo.find_by_profile_url(
        conn, "https://linkedin.com/in/ada", exclude_contact_id=CONTACT_ID
    )
    assert rows == [{"id": CONTACT_ID, "full_name": "Ada"}]
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "profile_url = %s" in sql and "id <> %s" in sql

    named = _conn([])
    assert (
        repo.find_by_name_company(
            named,
            full_name="Ada",
            company_id=COMPANY_ID,
            exclude_contact_id=CONTACT_ID,
        )
        == []
    )
    named_sql = str(named.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "LOWER(full_name) = LOWER(%s)" in named_sql and "id <> %s" in named_sql
