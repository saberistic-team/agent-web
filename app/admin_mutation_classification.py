"""Explicit audit/telemetry classification for admin unsafe routes (#334)."""

from __future__ import annotations

from typing import Iterator, Literal

from fastapi import FastAPI
from starlette.routing import BaseRoute

MutationClassification = Literal[
    "required_immutable_business_audit",
    "bounded_operational_security_telemetry",
    "intentionally_unaudited",
]

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# (HTTP method, route path template) -> (classification, documented reason)
ADMIN_MUTATION_ROUTE_CLASSIFICATIONS: dict[tuple[str, str], tuple[MutationClassification, str]] = {
    ("POST", "/admin/login"): (
        "required_immutable_business_audit",
        "Successful login emits required auth.login.success; failed attempts emit "
        "best-effort auth.login.failure as bounded operational telemetry.",
    ),
    ("POST", "/admin/logout"): (
        "required_immutable_business_audit",
        "Authenticated revocation emits required auth.logout; anonymous cookie cleanup "
        "performs no audit append.",
    ),
    ("POST", "/admin/companies"): (
        "intentionally_unaudited",
        "Company creation persists the canonical business row only; immutable audit "
        "covers updates via company.update.",
    ),
    ("POST", "/admin/companies/{company_id}/edit"): (
        "required_immutable_business_audit",
        "Company field updates emit company.update with bounded before/after summaries.",
    ),
    ("POST", "/admin/companies/{company_id}/archive"): (
        "intentionally_unaudited",
        "Archive toggles lifecycle columns on the canonical company row without an "
        "append-only audit event in this release.",
    ),
    ("POST", "/admin/companies/{company_id}/restore"): (
        "intentionally_unaudited",
        "Restore toggles lifecycle columns on the canonical company row without an "
        "append-only audit event in this release.",
    ),
    ("POST", "/admin/companies/{company_id}/research"): (
        "required_immutable_business_audit",
        "Research evidence append emits research_record.create with bounded metadata.",
    ),
    ("POST", "/admin/contacts"): (
        "intentionally_unaudited",
        "Contact creation persists the canonical business row only; immutable audit "
        "covers updates via contact.update.",
    ),
    ("POST", "/admin/contacts/{contact_id}/edit"): (
        "required_immutable_business_audit",
        "Contact field updates emit contact.update with bounded before/after summaries.",
    ),
    ("POST", "/admin/contacts/{contact_id}/archive"): (
        "intentionally_unaudited",
        "Archive toggles lifecycle columns on the canonical contact row without an "
        "append-only audit event in this release.",
    ),
    ("POST", "/admin/contacts/{contact_id}/restore"): (
        "required_immutable_business_audit",
        "Contact restore emits contact.restore with bounded before/after summaries.",
    ),
    ("POST", "/admin/contacts/{contact_id}/research"): (
        "required_immutable_business_audit",
        "Research evidence append emits research_record.create with bounded metadata.",
    ),
    ("POST", "/admin/briefs/{brief_id}/convert"): (
        "required_immutable_business_audit",
        "Brief conversion emits brief.convert with IDs and status only.",
    ),
    ("POST", "/admin/imports/batches/{batch_id}/rollback"): (
        "required_immutable_business_audit",
        "Import rollback emits import.batch.rollback with bounded batch metadata.",
    ),
    ("POST", "/admin/api/imports/linkedin/commit"): (
        "required_immutable_business_audit",
        "LinkedIn import commit emits import.batch with bounded batch metadata.",
    ),
    ("POST", "/admin/imports/reconcile-preview"): (
        "intentionally_unaudited",
        "Read-only reconciliation preview computes and returns a JSON diff without "
        "persisting anything; the eventual commit is covered by import.batch above.",
    ),
    ("POST", "/admin/pipeline/{company_id}/stage"): (
        "required_immutable_business_audit",
        "Pipeline stage transition emits pipeline.update with bounded summaries.",
    ),
    ("POST", "/admin/pipeline/{company_id}/next-action"): (
        "required_immutable_business_audit",
        "Pipeline next-action change emits pipeline.update with bounded summaries.",
    ),
    ("POST", "/admin/pipeline/{company_id}/activities"): (
        "required_immutable_business_audit",
        "Pipeline activity creation emits pipeline_activity.create with bounded metadata.",
    ),
    ("POST", "/admin/signals/rules"): (
        "required_immutable_business_audit",
        "Publishing an ICP rule version emits scoring_rule.update per changed rule with "
        "bounded before/after summaries.",
    ),
    ("POST", "/admin/signals/{company_id}/recalculate"): (
        "intentionally_unaudited",
        "Recalculation only inserts an append-only company_icp_score_snapshots row; no "
        "audit_events entry in this release.",
    ),
    ("POST", "/admin/signals/{company_id}/override"): (
        "intentionally_unaudited",
        "Override reason/actor are captured directly on the append-only "
        "company_icp_score_snapshots row; no separate audit_events entry in this release.",
    ),
}


def _iter_route_nodes(routes: list[BaseRoute], *, prefix: str = "") -> Iterator[tuple[str, set[str]]]:
    """Yield ``(path, methods)`` for concrete HTTP routes under nested routers."""
    for route in routes:
        route_type = type(route).__name__
        if route_type == "_IncludedRouter":
            include_prefix = route.include_context.prefix or ""
            yield from _iter_route_nodes(route.original_router.routes, prefix=include_prefix)
            continue
        if hasattr(route, "routes"):
            nested_prefix = prefix + (route.path if route.path != "/" else "")
            yield from _iter_route_nodes(route.routes, prefix=nested_prefix)
            continue
        if not hasattr(route, "methods"):
            continue
        path = prefix + route.path
        yield path, set(route.methods)


def iter_admin_unsafe_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Return every registered admin route that can mutate server state."""
    routes: set[tuple[str, str]] = set()
    for path, methods in _iter_route_nodes(app.router.routes):
        if not path.startswith("/admin"):
            continue
        for method in methods & _UNSAFE_METHODS:
            routes.add((method, path))
    return routes


def unclassified_admin_unsafe_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Routes missing from ADMIN_MUTATION_ROUTE_CLASSIFICATIONS, sorted for diffs."""
    registered = iter_admin_unsafe_routes(app)
    missing = registered - set(ADMIN_MUTATION_ROUTE_CLASSIFICATIONS)
    return sorted(missing)
