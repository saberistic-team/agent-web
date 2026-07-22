"""Unit tests for buying-group coverage and warm-introduction views (#124)."""

from __future__ import annotations

from datetime import date

import pytest

from app.admin_buying_group_pages import render_buying_group_section
from app.buying_group import (
    build_buying_group_view,
    build_warm_intro_paths,
    contact_coverage_status,
    safe_profile_link,
)

FOUNDER_ID = "11111111-1111-1111-1111-111111111101"
CTO_ID = "11111111-1111-1111-1111-111111111102"
VP_ID = "11111111-1111-1111-1111-111111111103"
INVESTOR_ID = "11111111-1111-1111-1111-111111111104"
INTRODUCER_ID = "11111111-1111-1111-1111-111111111105"
STALE_ID = "11111111-1111-1111-1111-111111111106"


def _contact(
    *,
    contact_id: str,
    roles: list[str],
    title: str = "Leader",
    email: str | None = "lead@acme.dev",
    email_permission: str = "permitted",
    relationship_strength: str = "warm",
    last_interaction_at: str | None = "2026-07-01",
    archived_at: str | None = None,
    profile_url: str | None = "https://linkedin.com/in/leader",
    full_name: str = "Pat Example",
) -> dict[str, object]:
    return {
        "id": contact_id,
        "full_name": full_name,
        "title": title,
        "email": email,
        "email_permission": email_permission,
        "buying_roles": roles,
        "relationship_strength": relationship_strength,
        "last_interaction_at": last_interaction_at,
        "archived_at": archived_at,
        "profile_url": profile_url,
    }


@pytest.mark.unit
def test_multi_role_contact_appears_in_multiple_slots() -> None:
    contacts = [
        _contact(
            contact_id=VP_ID,
            roles=["technical_buyer", "executive_buyer"],
            title="VP Engineering",
        )
    ]
    view = build_buying_group_view(contacts, [])
    slots = {slot.role_key: slot for slot in view.slots}
    assert len(slots["technical_buyer"].entries) == 1
    assert len(slots["executive_buyer"].entries) == 1
    assert slots["technical_buyer"].entries[0].contact_id == VP_ID
    assert slots["executive_buyer"].entries[0].contact_id == VP_ID
    assert slots["technical_buyer"].entries[0].also_roles == ("executive_buyer",)


@pytest.mark.unit
def test_stale_employment_from_title_and_departure_record() -> None:
    contact = _contact(
        contact_id=STALE_ID,
        roles=["technical_buyer"],
        title="Former CTO",
        email=None,
        email_permission="unknown",
        relationship_strength="cold",
    )
    records = [
        {
            "record_type": "verified_fact",
            "body": "CTO departed after platform reorg.",
            "contact_id": STALE_ID,
            "source_name": "Blog",
            "source_url": "https://acme.dev/blog/update",
            "observed_value": "departed",
            "observed_at": "2026-06-01T00:00:00Z",
            "confidence": 0.9,
            "review_at": "2026-08-01T00:00:00Z",
            "expires_at": "2026-12-01T00:00:00Z",
        }
    ]
    status, note = contact_coverage_status(contact, "technical_buyer", records=records)
    assert status == "stale_employment"
    assert note is not None


@pytest.mark.unit
def test_investor_requires_sourced_evidence_for_confirmed() -> None:
    investor = _contact(
        contact_id=INVESTOR_ID,
        roles=["investor"],
        title="Partner",
        email=None,
        email_permission="unknown",
        relationship_strength="developing",
        full_name="Riley Park",
    )
    possible_status, _ = contact_coverage_status(investor, "investor", records=[])
    assert possible_status == "possible"

    records = [
        {
            "record_type": "public_signal",
            "body": "Riley Park listed as investor in the seed round.",
            "contact_id": INVESTOR_ID,
            "source_name": "Crunchbase",
            "source_url": "https://crunchbase.com/acme",
            "observed_value": "investor",
            "observed_at": "2026-05-01T00:00:00Z",
            "confidence": 0.8,
            "review_at": "2026-07-01T00:00:00Z",
            "expires_at": "2026-10-01T00:00:00Z",
        }
    ]
    confirmed_status, note = contact_coverage_status(
        investor, "investor", records=records
    )
    assert confirmed_status == "confirmed"
    assert "sourced evidence" in (note or "")


@pytest.mark.unit
def test_missing_roles_render_as_research_gaps() -> None:
    contacts = [
        _contact(contact_id=FOUNDER_ID, roles=["founder"]),
    ]
    html = render_buying_group_section(contacts, [])
    assert "Research gap" in html
    assert "inventing a placeholder contact" in html
    view = build_buying_group_view(contacts, [])
    missing_slots = [slot for slot in view.slots if slot.slot_status == "missing"]
    assert {slot.role_key for slot in missing_slots} >= {
        "technical_buyer",
        "executive_buyer",
        "investor",
        "introducer",
    }


@pytest.mark.unit
def test_warm_intro_paths_include_context_and_metrics() -> None:
    contacts = [
        _contact(
            contact_id=INTRODUCER_ID,
            roles=["introducer"],
            title="Advisor",
            full_name="Avery Silva",
            last_interaction_at=date(2026, 7, 8),
            relationship_strength="warm",
        )
    ]
    records = [
        {
            "record_type": "relationship_context",
            "body": "Former colleague — worked together at Cedar Protocol.",
            "contact_id": INTRODUCER_ID,
        }
    ]
    paths = build_warm_intro_paths(contacts, records)
    assert len(paths) == 1
    assert "Former colleague" in paths[0].relationship_context
    assert "Relationship: Warm" in paths[0].interaction_metrics
    assert "Last interaction: 2026-07-08" in paths[0].interaction_metrics

    html = render_buying_group_section(contacts, records)
    assert "Warm introduction paths" in html
    assert "Former colleague" in html
    assert "Interaction metrics" in html


@pytest.mark.unit
def test_safe_profile_link_rejects_javascript_urls() -> None:
    assert "javascript:" not in safe_profile_link("javascript:alert(1)", label="Bad")
    rendered = safe_profile_link("javascript:alert(1)", label="Bad")
    assert "<a " not in rendered
    assert "Bad" in rendered

    safe = safe_profile_link("https://linkedin.com/in/safe", label="Profile")
    assert 'href="https://linkedin.com/in/safe"' in safe
    assert 'rel="noopener noreferrer"' in safe


@pytest.mark.unit
def test_buying_group_section_renders_role_groups_and_badges() -> None:
    contacts = [
        _contact(contact_id=FOUNDER_ID, roles=["founder"]),
        _contact(
            contact_id=CTO_ID,
            roles=["technical_buyer"],
            title="CTO",
            relationship_strength="strong",
        ),
        _contact(
            contact_id=INVESTOR_ID,
            roles=["investor"],
            title="Partner",
            email=None,
            email_permission="unknown",
            relationship_strength="developing",
            full_name="Riley Park",
        ),
    ]
    html = render_buying_group_section(contacts, [])
    assert "Buying-group coverage" in html
    assert "Founder" in html
    assert "CTO" in html
    assert "VP Engineering" in html
    assert "Confirmed contact" in html
    assert "Possible contact" in html
    assert 'class="buying-profile-link"' in html
