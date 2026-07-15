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
    find_email_duplicate_warnings,
    find_name_company_duplicate_warnings,
    find_profile_url_duplicate_warnings,
    merge_duplicate_warnings,
    normalize_email,
    normalize_profile_url,
)
from app.repositories.postgres import PostgresContactRepository

pytestmark = [pytest.mark.unit, pytest.mark.integration]

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    cursor.fetchone.return_value = rows[0] if rows else None
    return conn


@pytest.mark.unit
def test_profile_and_email_normalization() -> None:
    assert normalize_profile_url(" HTTPS://WWW.LinkedIn.com/in/Example/ ") == "linkedin.com/in/Example"
    assert normalize_email(" Lead@Example.COM ") == "lead@example.com"
    with pytest.raises(ValueError, match="valid URL"):
        normalize_profile_url("://missing-host")
    contact = ContactCreate(
        full_name=" Ada Lovelace ",
        company_id=COMPANY_ID,
        profile_url="linkedin.com/in/ada",
        email="Ada@Example.com",
        buying_roles=["founder", "technical_buyer", "founder"],
    )
    assert contact.full_name == "Ada Lovelace"
    assert contact.profile_url == "linkedin.com/in/ada"
    assert contact.email == "ada@example.com"
    assert contact.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_duplicate_warning_helpers_ignore_archived_and_excluded_contact() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "linkedin.com/in/ada",
            "company_id": COMPANY_ID,
            "archived_at": "2026-01-01",
        },
        {
            "id": UUID("33333333-3333-3333-3333-333333333333"),
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "linkedin.com/in/ada",
            "company_id": COMPANY_ID,
        },
    ]
    assert (
        find_profile_url_duplicate_warnings(
            contacts,
            profile_url="linkedin.com/in/ada",
            exclude_contact_id=UUID("33333333-3333-3333-3333-333333333333"),
        )
        == []
    )
    assert (
        find_email_duplicate_warnings(
            contacts,
            email="ada@example.com",
            exclude_contact_id=UUID("33333333-3333-3333-3333-333333333333"),
        )
        == []
    )


@pytest.mark.unit
def test_contact_repository_crud_and_role_assignment() -> None:
    repo = PostgresContactRepository()
    row = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "company_id": COMPANY_ID,
        "title": "CTO",
        "profile_url": "linkedin.com/in/ada",
    }
    conn = _conn([row])
    created = repo.create(
        conn,
        full_name="Ada Lovelace",
        company_id=COMPANY_ID,
        email="ada@example.com",
        buying_roles=["founder", "technical_buyer"],
    )
    assert created["buying_roles"] == ["founder", "technical_buyer"]

    lookup = _conn([row])
    lookup.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        {"contact_id": CONTACT_ID, "role": "founder"}
    ]
    assert repo.get_by_id(lookup, CONTACT_ID)["full_name"] == "Ada Lovelace"

    missing = _conn(None)
    missing.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    assert repo.get_by_email(missing, "missing@example.com") is None

    update_conn = _conn([row])
    update_conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [row, row]
    update_conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        {"contact_id": CONTACT_ID, "role": "investor"}
    ]
    updated = repo.update(
        update_conn,
        CONTACT_ID,
        buying_roles=["investor"],
    )
    assert updated is not None

    restore_conn = _conn([row])
    restore_conn.cursor.return_value.__enter__.return_value.fetchone.return_value = row
    restore_conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    restored = repo.restore(restore_conn, CONTACT_ID)
    assert restored is not None

    profile_conn = _conn([row])
    profile_conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = [[row], []]
    assert len(repo.find_by_profile_url(profile_conn, "linkedin.com/in/ada")) == 1

    name_conn = _conn([row])
    name_conn.cursor.return_value.__enter__.return_value.fetchall.side_effect = [[row], []]
    assert len(
        repo.find_by_name_company(
            name_conn,
            full_name="Ada Lovelace",
            company_id=COMPANY_ID,
        )
    ) == 1


@pytest.mark.unit
def test_contact_admin_form_renders_archive_and_restore_actions() -> None:
    company = {"id": COMPANY_ID, "name": "Acme"}
    archived = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "company_id": COMPANY_ID,
        "archived_at": "2026-07-14",
        "buying_roles": [],
    }
    archived_page = render_contact_form_page(
        contact=archived,
        companies=[company],
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Restore contact" in archived_page
    warning_page = render_contact_form_page(
        contact=archived,
        companies=[company],
        csrf_token="csrf",
        admin_username="admin",
        warnings=[
            type("Warn", (), {"contact_id": str(CONTACT_ID), "full_name": "Ada", "reason": "email"})()
        ],
    )
    assert "email already matches" in warning_page


@pytest.mark.unit
def test_unknown_roles_and_email_provenance_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown buying role"):
        ContactCreate(full_name="Ada", company_id=COMPANY_ID, buying_roles=["ceo"])
    with pytest.raises(ValidationError, match="email provenance requires"):
        ContactCreate(
            full_name="Ada",
            company_id=COMPANY_ID,
            email_provenance="manual",
        )


@pytest.mark.unit
def test_duplicate_warnings_merge_profile_email_and_name_company() -> None:
    contacts = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "linkedin.com/in/ada",
            "company_id": COMPANY_ID,
        }
    ]
    warnings = merge_duplicate_warnings(
        find_profile_url_duplicate_warnings(
            contacts, profile_url="https://www.linkedin.com/in/ada"
        ),
        find_email_duplicate_warnings(contacts, email="ADA@example.com"),
        find_name_company_duplicate_warnings(
            contacts, full_name="ada lovelace", company_id=COMPANY_ID
        ),
    )
    assert len(warnings) == 3
    assert {item.reason for item in warnings} == {"profile URL", "email", "name at company"}


@pytest.mark.unit
def test_contact_repository_search_filters_and_archive_are_non_destructive() -> None:
    repo = PostgresContactRepository()
    conn = _conn()
    repo.list_all(
        conn,
        query="ada",
        company_id=COMPANY_ID,
        buying_role="founder",
        relationship_strength="warm",
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "full_name ILIKE" in sql
    assert "contact_buying_roles" in sql
    assert "archived_at IS NULL" in sql

    archived = _conn([{"id": CONTACT_ID}])
    archived.cursor.return_value.__enter__.return_value.fetchone.return_value = {"id": CONTACT_ID}
    archived.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    repo.archive(archived, CONTACT_ID)
    execute_calls = archived.cursor.return_value.__enter__.return_value.execute.call_args_list
    archive_sql = str(execute_calls[0].args[0])
    assert "UPDATE contacts SET archived_at" in archive_sql
    assert "DELETE" not in archive_sql


@pytest.mark.unit
def test_contact_admin_pages_render_roles_and_warnings() -> None:
    company = {"id": COMPANY_ID, "name": "Acme"}
    contact = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "CTO",
        "company_id": COMPANY_ID,
        "email": "ada@acme.dev",
        "buying_roles": ["technical_buyer"],
        "last_interaction_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
    }
    listing = render_contacts_list_page(
        contacts=[contact],
        filters={"q": "Ada", "company_id": None, "buying_role": None, "relationship_strength": None, "archived": None},
        companies=[company],
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add contact" in listing and "Technical buyer" in listing
    edit = render_contact_form_page(
        contact=contact,
        companies=[company],
        csrf_token="csrf",
        admin_username="admin",
        error_message="warning",
    )
    assert "/edit" in edit and "warning" in edit and "Technical buyer" in edit
