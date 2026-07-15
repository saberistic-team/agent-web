"""Unit tests for admin contact page rendering."""

from __future__ import annotations

from uuid import UUID

import pytest

from app import admin_contacts
from app.contacts import ContactDuplicateWarning


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
def test_render_contacts_list_page_includes_search_and_rows() -> None:
    html = admin_contacts.render_contacts_list_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[{"id": COMPANY_ID, "name": "Acme"}],
        contacts=[
            {
                "id": CONTACT_ID,
                "full_name": "Pat Example",
                "title": "CTO",
                "company_id": COMPANY_ID,
                "buying_roles": ["technical_buyer"],
                "email": "pat@acme.dev",
                "last_interaction_at": None,
            }
        ],
        filters={"q": "pat", "buying_role": None, "company_id": None, "archived": None},
    )
    assert "Contacts" in html
    assert "Pat Example" in html
    assert "Technical buyer" in html
    assert 'value="pat"' in html
    assert 'href="/admin/contacts/new"' in html


@pytest.mark.unit
def test_render_contact_form_page_new_and_edit() -> None:
    companies = [{"id": COMPANY_ID, "name": "Acme"}]
    new_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=companies,
        contact=None,
    )
    assert "Add contact" in new_html
    assert 'action="/admin/contacts"' in new_html
    assert "Founder" in new_html

    edit_html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=companies,
        contact={
            "id": CONTACT_ID,
            "full_name": "Pat",
            "company_id": COMPANY_ID,
            "buying_roles": ["founder", "investor"],
        },
        warnings=[
            ContactDuplicateWarning(
                contact_id=str(CONTACT_ID),
                label="Other Pat",
                match_type="email",
            )
        ],
    )
    assert "Edit Pat" in edit_html
    assert f"/admin/contacts/{CONTACT_ID}/edit" in edit_html
    assert "Archive contact" in edit_html
    assert 'class="admin-action-button admin-action-destructive"' in edit_html
    assert "Possible duplicate" in edit_html


@pytest.mark.unit
def test_render_contact_form_page_shows_archived_state() -> None:
    html = admin_contacts.render_contact_form_page(
        admin_username="operator",
        csrf_token="csrf-token",
        companies=[],
        contact={"id": CONTACT_ID, "full_name": "Pat", "archived_at": "2026-01-01"},
    )
    assert "Restore contact" in html
    assert 'class="admin-action-button admin-action-restore"' in html
