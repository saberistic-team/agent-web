"""Unit tests for admin company list rendering."""

from __future__ import annotations

from uuid import UUID

import pytest

from app import admin_companies


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.mark.unit
@pytest.mark.integration
def test_render_companies_list_page() -> None:
    html = admin_companies.render_companies_list_page(
        admin_username="operator",
        csrf_token="csrf-token",
        filters={
            "q": None,
            "category": None,
            "stage": None,
            "target_status": None,
            "freshness": None,
            "archived": None,
        },
        companies=[
            {
                "id": COMPANY_ID,
                "name": "Acme",
                "website": "https://acme.dev",
                "category": "fintech",
                "stage": "seed",
                "target_status": "target",
                "last_verified_at": None,
            }
        ],
    )
    assert "Companies" in html
    assert "Acme" in html
    assert f"/admin/companies/{COMPANY_ID}" in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_companies_list_page_empty() -> None:
    html = admin_companies.render_companies_list_page(
        admin_username="operator",
        csrf_token="csrf-token",
        filters={
            "q": None,
            "category": None,
            "stage": None,
            "target_status": None,
            "freshness": None,
            "archived": None,
        },
        companies=[],
    )
    assert "No companies match these filters." in html
