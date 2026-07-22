"""Coverage contract for admin unsafe route CSRF classification (#329)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.admin_csrf_classification import (
    ADMIN_CSRF_ROUTE_POLICIES,
    iter_admin_state_changing_routes,
    stale_admin_csrf_route_entries,
    unclassified_admin_csrf_routes,
)
from app.admin_mutation_classification import iter_admin_unsafe_routes
from app.main import app


@pytest.mark.unit
def test_every_admin_unsafe_route_has_csrf_policy() -> None:
    registered = iter_admin_unsafe_routes(app)
    missing = registered - set(ADMIN_CSRF_ROUTE_POLICIES)
    assert not missing, f"Unclassified admin CSRF routes: {sorted(missing)}"


@pytest.mark.unit
def test_csrf_policy_table_has_no_stale_entries() -> None:
    stale = stale_admin_csrf_route_entries(app)
    assert not stale, f"Stale CSRF policy entries: {sorted(stale)}"


@pytest.mark.unit
def test_state_changing_routes_require_session_or_login_csrf() -> None:
    for route in iter_admin_state_changing_routes(app):
        policy, reason = ADMIN_CSRF_ROUTE_POLICIES[route]
        assert policy in {
            "login_flow_csrf",
            "session_csrf_required",
            "session_csrf_when_authenticated",
        }, route
        assert reason.strip(), route


@pytest.mark.unit
def test_unclassified_helper_reports_synthetic_route() -> None:
    probe = FastAPI()

    @probe.post("/admin/synthetic/unclassified")
    def _synthetic() -> dict[str, str]:
        return {"ok": "true"}

    missing = unclassified_admin_csrf_routes(probe)
    assert ("POST", "/admin/synthetic/unclassified") in missing


@pytest.mark.unit
def test_linkedin_commit_policy_documents_header_transport() -> None:
    policy, reason = ADMIN_CSRF_ROUTE_POLICIES[
        ("POST", "/admin/api/imports/linkedin/commit")
    ]
    assert policy == "session_csrf_required"
    assert "X-CSRF-Token" in reason
