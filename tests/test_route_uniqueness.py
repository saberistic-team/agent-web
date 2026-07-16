"""Route-table and OpenAPI uniqueness guard (issue #310)."""

from __future__ import annotations


import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRouter

from app import admin_routes
from app.admin_response_policy import (
    ADMIN_BROWSER_SECURITY_HEADERS,
    ADMIN_NO_STORE_HEADERS,
)
from app.main import app
from app.route_uniqueness import (
    OPENAPI_GET_HEAD_SAME_PATH_REASON,
    RouteUniquenessError,
    collect_openapi_operation_ids,
    find_method_path_duplicates,
    find_openapi_operation_id_duplicates,
    find_route_name_collisions,
    format_method_path_duplicate_error,
    iter_route_registrations,
    validate_app_routes,
    validate_method_path_uniqueness,
)


@pytest.mark.unit
def test_production_app_has_no_duplicate_method_path_pairs() -> None:
    validate_app_routes(app)


@pytest.mark.unit
def test_synthetic_duplicate_method_path_reports_actionable_diagnostics() -> None:
    fixture = FastAPI()

    @fixture.get("/dup")
    def first_dup() -> dict[str, str]:
        return {"handler": "first"}

    @fixture.get("/dup")
    def second_dup() -> dict[str, str]:
        return {"handler": "second"}

    registrations = iter_route_registrations(fixture)
    duplicates = find_method_path_duplicates(registrations)
    assert ("GET", "/dup") in duplicates
    message = format_method_path_duplicate_error(duplicates)
    assert "GET '/dup'" in message or 'GET "/dup"' in message or "GET /dup" in message
    assert "first_dup" in message
    assert "second_dup" in message
    assert "position=" in message
    with pytest.raises(RouteUniquenessError, match="Duplicate HTTP method/path"):
        validate_method_path_uniqueness(fixture)


@pytest.mark.unit
def test_same_path_disjoint_methods_is_allowed() -> None:
    fixture = FastAPI()

    @fixture.get("/resource")
    def read_resource() -> dict[str, str]:
        return {"method": "get"}

    @fixture.post("/resource")
    def write_resource() -> dict[str, str]:
        return {"method": "post"}

    validate_method_path_uniqueness(fixture)


@pytest.mark.unit
def test_head_on_get_route_does_not_create_false_positive() -> None:
    fixture = FastAPI()

    @fixture.api_route("/head-safe", methods=["GET", "HEAD"])
    def head_safe() -> dict[str, str]:
        return {"ok": "1"}

    registrations = iter_route_registrations(fixture)
    duplicates = find_method_path_duplicates(registrations)
    assert duplicates == {}
    validate_method_path_uniqueness(fixture)


@pytest.mark.unit
def test_mounted_router_duplicate_is_detected() -> None:
    child = APIRouter()

    @child.get("/child")
    def child_once() -> dict[str, str]:
        return {"n": "1"}

    @child.get("/child")
    def child_twice() -> dict[str, str]:
        return {"n": "2"}

    fixture = FastAPI()
    fixture.include_router(child, prefix="/mounted")
    with pytest.raises(RouteUniquenessError, match="/mounted/child"):
        validate_method_path_uniqueness(fixture)


@pytest.mark.unit
def test_duplicate_route_name_collision_is_detected() -> None:
    fixture = FastAPI()

    @fixture.get("/alpha", name="shared_name")
    def alpha_handler() -> dict[str, str]:
        return {"where": "alpha"}

    @fixture.get("/beta", name="shared_name")
    def beta_handler() -> dict[str, str]:
        return {"where": "beta"}

    registrations = iter_route_registrations(fixture)
    collisions = find_route_name_collisions(registrations)
    assert "shared_name" in collisions
    assert len(collisions["shared_name"]) == 2


@pytest.mark.unit
def test_openapi_operation_ids_are_unique_and_deterministic() -> None:
    first = collect_openapi_operation_ids(app)
    second = collect_openapi_operation_ids(app)
    assert first == second
    assert first
    assert find_openapi_operation_id_duplicates(app) == {}
    validate_app_routes(app)


@pytest.mark.unit
def test_openapi_get_head_same_path_framework_pair_is_documented() -> None:
    assert "GET and HEAD" in OPENAPI_GET_HEAD_SAME_PATH_REASON
    assert find_openapi_operation_id_duplicates(app) == {}


@pytest.mark.unit
def test_second_audit_route_mutation_fails_guard() -> None:
    duplicate_router = APIRouter(prefix="/admin")

    @duplicate_router.get("/audit", response_class=HTMLResponse)
    def duplicate_admin_audit_list() -> HTMLResponse:
        return HTMLResponse("duplicate")

    probe = FastAPI()
    probe.include_router(admin_routes.router)
    probe.include_router(duplicate_router)
    with pytest.raises(RouteUniquenessError, match="/admin/audit"):
        validate_method_path_uniqueness(probe)
