"""Tests for computed vs human judgment contact metrics UI."""

from __future__ import annotations

import random
from uuid import UUID

import pytest

from app.admin_contact_metrics import (
    render_computed_linkedin_metrics_panel,
    render_human_judgment_panel,
)
from app.admin_preview import (
    PREVIEW_CONTACT_POPULATED_ID,
    build_preview_contact,
    build_preview_linkedin_metrics,
)
from app.admin_preview_context import preview_reference_time

pytestmark = pytest.mark.unit


def test_computed_metrics_panel_labels_source() -> None:
    now = preview_reference_time()
    contact = build_preview_contact(PREVIEW_CONTACT_POPULATED_ID, rng=random.Random(7), now=now)
    assert contact is not None
    html = render_computed_linkedin_metrics_panel(contact)
    assert "Computed relationship metrics" in html
    assert "never stored" in html
    assert "Scoring inputs (deterministic)" in html
    assert str(contact["linkedin_metrics"]["computed_score"]) in html


def test_human_judgment_panel_separate_from_computed() -> None:
    from app.admin_research_pages import render_admin_contact_research_page

    contact = {
        "id": "00000000-0000-0000-0000-000000000001",
        "relationship_strength": "warm",
        "last_interaction_at": "2026-07-01",
        "former_colleague": True,
        "warm_introducer": False,
        "buying_roles": ["introducer"],
        "notes": "Trusted operator context",
        "full_name": "Preview Contact",
        "linkedin_metrics": build_preview_linkedin_metrics(
            rng=random.Random(3),
            now=preview_reference_time(),
            former_colleague=True,
        ),
    }
    computed = render_computed_linkedin_metrics_panel(contact)
    human = render_human_judgment_panel(contact)
    page = render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="preview",
    )
    assert "Operator judgment" in human
    assert "Not inferred from private messages" in human
    assert "Former colleague" in human
    assert "Computed relationship metrics" in computed
    assert page.index("Computed relationship metrics") < page.index("Operator judgment")
