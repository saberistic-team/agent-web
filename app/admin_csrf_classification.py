"""Explicit CSRF policy classification for admin unsafe routes (#329)."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI

from app.admin_mutation_classification import iter_admin_unsafe_routes

CsrfPolicy = Literal[
    "login_flow_csrf",
    "session_csrf_when_authenticated",
    "session_csrf_required",
    "exempt_read_only_preview",
]

_READ_ONLY_PREVIEW_ROUTES = frozenset({
    ("POST", "/admin/imports/reconcile-preview"),
    ("POST", "/admin/discovery/bulk/preview"),
})

# (HTTP method, route path template) -> (policy, documented reason)
ADMIN_CSRF_ROUTE_POLICIES: dict[tuple[str, str], tuple[CsrfPolicy, str]] = {
    ("POST", "/admin/login"): (
        "login_flow_csrf",
        "Pre-authentication login POST requires a flow-bound synchronizer token in the form body.",
    ),
    ("POST", "/admin/logout"): (
        "session_csrf_when_authenticated",
        "Live sessions require a session-bound CSRF token in the form body; anonymous logout is idempotent.",
    ),
    ("POST", "/admin/companies"): (
        "session_csrf_required",
        "Company creation requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/companies/{company_id}/edit"): (
        "session_csrf_required",
        "Company updates require a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/companies/{company_id}/archive"): (
        "session_csrf_required",
        "Company archive requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/companies/{company_id}/restore"): (
        "session_csrf_required",
        "Company restore requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/companies/{company_id}/research"): (
        "session_csrf_required",
        "Research append requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/contacts"): (
        "session_csrf_required",
        "Contact creation requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/contacts/{contact_id}/edit"): (
        "session_csrf_required",
        "Contact updates require a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/contacts/{contact_id}/archive"): (
        "session_csrf_required",
        "Contact archive requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/contacts/{contact_id}/restore"): (
        "session_csrf_required",
        "Contact restore requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/contacts/{contact_id}/research"): (
        "session_csrf_required",
        "Research append requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/briefs/{brief_id}/convert"): (
        "session_csrf_required",
        "Brief conversion requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/imports/batches/{batch_id}/rollback"): (
        "session_csrf_required",
        "Import rollback requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/api/imports/linkedin/commit"): (
        "session_csrf_required",
        "LinkedIn import commit requires the session-bound CSRF token in the X-CSRF-Token header "
        "before JSON parsing or persistence.",
    ),
    ("POST", "/admin/imports/reconcile-preview"): (
        "exempt_read_only_preview",
        "Read-only reconciliation preview performs no persistence; preview mode denies unsafe methods.",
    ),
    ("POST", "/admin/pipeline/{company_id}/stage"): (
        "session_csrf_required",
        "Pipeline stage transition requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/pipeline/{company_id}/next-action"): (
        "session_csrf_required",
        "Pipeline next-action change requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/pipeline/{company_id}/activities"): (
        "session_csrf_required",
        "Pipeline activity creation requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/signals/rules"): (
        "session_csrf_required",
        "ICP rule publish requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/signals/{company_id}/recalculate"): (
        "session_csrf_required",
        "ICP score recalculation requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/signals/{company_id}/override"): (
        "session_csrf_required",
        "ICP score override requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/targets/working-list"): (
        "session_csrf_required",
        "Qualification working-list save requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/discovery/{candidate_id}/accept"): (
        "session_csrf_required",
        "Discovery accept requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/discovery/{candidate_id}/reject"): (
        "session_csrf_required",
        "Discovery reject requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/discovery/{candidate_id}/defer"): (
        "session_csrf_required",
        "Discovery defer requires a session-bound CSRF token in the form body.",
    ),
    ("POST", "/admin/discovery/bulk/preview"): (
        "exempt_read_only_preview",
        "Bulk preview performs no persistence; preview mode denies unsafe methods except allowlisted read-only previews.",
    ),
    ("POST", "/admin/discovery/bulk/commit"): (
        "session_csrf_required",
        "Bulk commit requires a session-bound CSRF token in the form body.",
    ),
}


def iter_admin_state_changing_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Return registered admin routes that mutate server state."""
    return iter_admin_unsafe_routes(app) - _READ_ONLY_PREVIEW_ROUTES


def unclassified_admin_csrf_routes(app: FastAPI) -> list[tuple[str, str]]:
    """State-changing routes missing from ADMIN_CSRF_ROUTE_POLICIES, sorted for diffs."""
    registered = iter_admin_state_changing_routes(app)
    missing = registered - set(ADMIN_CSRF_ROUTE_POLICIES)
    return sorted(missing)


def stale_admin_csrf_route_entries(app: FastAPI) -> list[tuple[str, str]]:
    """Classification entries that no longer match a registered route."""
    registered = iter_admin_unsafe_routes(app)
    stale = set(ADMIN_CSRF_ROUTE_POLICIES) - registered
    return sorted(stale)
