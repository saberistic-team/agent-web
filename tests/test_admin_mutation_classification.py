"""Coverage contract for admin unsafe route audit/telemetry classification (#334)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from app.admin_mutation_classification import (
    ADMIN_MUTATION_ROUTE_CLASSIFICATIONS,
    iter_admin_unsafe_routes,
    unclassified_admin_unsafe_routes,
)
from app.main import app


@pytest.mark.unit
def test_every_admin_unsafe_route_is_classified() -> None:
    registered = iter_admin_unsafe_routes(app)
    missing = registered - set(ADMIN_MUTATION_ROUTE_CLASSIFICATIONS)
    assert not missing, f"Unclassified admin unsafe routes: {sorted(missing)}"


@pytest.mark.unit
def test_classification_table_has_no_stale_entries() -> None:
    registered = iter_admin_unsafe_routes(app)
    stale = set(ADMIN_MUTATION_ROUTE_CLASSIFICATIONS) - registered
    assert not stale, f"Stale classification entries: {sorted(stale)}"


@pytest.mark.unit
def test_unclassified_helper_reports_synthetic_route() -> None:
    probe = FastAPI()

    @probe.post("/admin/synthetic/unclassified")
    def _synthetic() -> dict[str, str]:
        return {"ok": "true"}

    missing = unclassified_admin_unsafe_routes(probe)
    assert ("POST", "/admin/synthetic/unclassified") in missing


@pytest.mark.unit
def test_classification_reasons_are_non_empty() -> None:
    for key, (classification, reason) in ADMIN_MUTATION_ROUTE_CLASSIFICATIONS.items():
        assert classification in {
            "required_immutable_business_audit",
            "bounded_operational_security_telemetry",
            "intentionally_unaudited",
        }, key
        assert reason.strip(), key


@pytest.mark.unit
def test_research_and_pipeline_routes_require_business_audit() -> None:
    audited = {
        key
        for key, (classification, _reason) in ADMIN_MUTATION_ROUTE_CLASSIFICATIONS.items()
        if classification == "required_immutable_business_audit"
    }
    assert ("POST", "/admin/companies/{company_id}/research") in audited
    assert ("POST", "/admin/contacts/{contact_id}/research") in audited
    assert ("POST", "/admin/pipeline/{company_id}/activities") in audited
