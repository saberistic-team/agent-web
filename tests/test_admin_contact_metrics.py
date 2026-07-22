"""Tests for computed vs human judgment contact metrics UI."""

from __future__ import annotations

import random

import pytest

from app.admin_contact_metrics import (
    render_computed_relationship_metrics,
    render_operator_judgment_fields,
)
from app.admin_preview import (
    PREVIEW_CONTACT_POPULATED_ID,
    build_preview_contact,
)
from app.admin_preview_context import preview_reference_time
from app.admin_research_pages import render_admin_contact_research_page

pytestmark = pytest.mark.unit


def test_computed_metrics_panel_labels_source() -> None:
    now = preview_reference_time()
    contact = build_preview_contact(PREVIEW_CONTACT_POPULATED_ID, rng=random.Random(7), now=now)
    assert contact is not None
    metrics = contact["relationship_metrics"]
    assert isinstance(metrics, dict)
    html = render_computed_relationship_metrics(metrics)
    assert "LinkedIn-derived metrics" in html
    assert "Computed from export metadata only" in html
    assert "Scoring inputs (deterministic)" in html
    assert str(metrics["message_count"]) in html


def test_human_judgment_panel_separate_from_computed() -> None:
    contact = {
        "id": "00000000-0000-0000-0000-000000000001",
        "relationship_strength": "warm",
        "last_interaction_at": "2026-07-01",
        "crm_context_tags": ["former_colleague"],
        "buying_roles": ["introducer"],
        "notes": "Trusted operator context",
        "full_name": "Preview Contact",
        "relationship_metrics": {
            "message_count": 3,
            "inbound_count": 1,
            "outbound_count": 2,
            "conversation_count": 1,
            "recent_interaction_30d": True,
            "recent_interaction_90d": True,
            "two_way_conversation": True,
            "scoring_inputs": {"message_count": 3},
        },
    }
    computed = render_computed_relationship_metrics(contact["relationship_metrics"])
    human = render_operator_judgment_fields(contact, crm_context_checkboxes="")
    page = render_admin_contact_research_page(
        contact=contact,
        company=None,
        records=[],
        csrf_token="csrf",
        admin_username="preview",
    )
    assert "Operator judgment" in human
    assert "Human-assigned context" in human
    assert "Former colleague" in human
    assert "LinkedIn-derived metrics" in computed
    assert page.index("LinkedIn-derived metrics") < page.index("Operator judgment")
