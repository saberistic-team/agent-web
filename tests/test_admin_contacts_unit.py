"""Unit tests for admin contact page rendering."""

from __future__ import annotations

from uuid import UUID

import pytest

from app import admin_contacts


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.unit
@pytest.mark.integration
def test_render_contacts_list_page_includes_search_and_rows() -> None:
    html = admin_contacts.render_contacts_list_page(
        contacts=[
            {
                "id": CONTACT_ID,
                "name": "Pat Example",
                "title": "CTO",
                "company_name": "Acme",
                "buying_roles": ["technical_buyer"],
                "is_archived": False,
            }
        ],
        query="pat",
        include_archived=False,
        warnings=["Email matches existing contact: Sam"],
    )
    assert "Contacts" in html
    assert "Pat Example" in html
    assert "Technical buyer" in html
    assert "Possible duplicates" in html
    assert 'value="pat"' in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_contact_form_page_new_and_edit() -> None:
    companies = [{"id": COMPANY_ID, "name": "Acme"}]
    new_html = admin_contacts.render_contact_form_page(
        companies=companies,
        is_new=True,
    )
    assert "New contact" in new_html
    assert 'action="/admin/contacts/new"' in new_html
    assert "Founder" in new_html

    edit_html = admin_contacts.render_contact_form_page(
        companies=companies,
        contact={
            "id": CONTACT_ID,
            "name": "Pat",
            "company_id": COMPANY_ID,
            "buying_roles": ["founder", "investor"],
            "is_archived": False,
        },
    )
    assert "Edit contact" in edit_html
    assert f'/admin/contacts/{CONTACT_ID}' in edit_html
    assert "Archive" in edit_html


@pytest.mark.unit
@pytest.mark.integration
def test_render_contact_form_page_shows_archived_state() -> None:
    html = admin_contacts.render_contact_form_page(
        companies=[],
        contact={"id": CONTACT_ID, "name": "Pat", "is_archived": True},
    )
    assert "archived" in html.lower()
    assert "Restore" in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_company_detail_page_lists_contacts() -> None:
    html = admin_contacts.render_company_detail_page(
        company={"id": COMPANY_ID, "name": "Acme", "website": "https://acme.dev", "status": "prospect"},
        contacts=[
            {
                "id": CONTACT_ID,
                "name": "Pat",
                "title": "CEO",
                "buying_roles": ["founder"],
                "relationship_strength": "strong",
            }
        ],
    )
    assert "Associated contacts" in html
    assert "Pat" in html
    assert "Strong" in html


@pytest.mark.unit
@pytest.mark.integration
def test_parse_contact_form_normalizes_fields() -> None:
    parsed = admin_contacts.parse_contact_form(
        name="  Pat  ",
        title=" CTO ",
        profile_url=" https://linkedin.com/in/pat ",
        company_id=str(COMPANY_ID),
        email=" Pat@Example.com ",
        email_permission="permitted",
        email_provenance="intro",
        last_interaction_at="2026-01-15",
        relationship_strength="good",
        notes=" met at event ",
        buying_roles=["founder", "founder", "other"],
    )
    assert parsed["name"] == "Pat"
    assert parsed["company_id"] == COMPANY_ID
    assert parsed["email"] == "Pat@Example.com"
    assert parsed["buying_roles"] == ["founder", "other"]
    assert parsed["relationship_strength"] == "good"
