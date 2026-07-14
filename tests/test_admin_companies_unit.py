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
        companies=[
            {
                "id": COMPANY_ID,
                "name": "Acme",
                "website": "https://acme.dev",
                "status": "prospect",
            }
        ]
    )
    assert "Companies" in html
    assert "Acme" in html
    assert f"/admin/companies/{COMPANY_ID}" in html


@pytest.mark.unit
@pytest.mark.integration
def test_render_companies_list_page_empty() -> None:
    html = admin_companies.render_companies_list_page(companies=[])
    assert "No companies yet" in html
