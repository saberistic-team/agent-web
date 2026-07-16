"""Route-table and OpenAPI uniqueness guard tests (issue #310)."""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRouter
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.main import app
from app.route_uniqueness import (
    RouteUniquenessError,
    assert_openapi_operation_ids_deterministic,
    collect_route_registrations,
    validate_application_routes,
)


@pytest.mark.unit
def test_production_route_tree_has_no_duplicate_method_path_pairs() -> None:
    validate_application_routes(app)


@pytest.mark.unit
def test_synthetic_duplicate_method_path_fails_with_actionable_diagnostics() -> None:
    probe = FastAPI()

    @probe.get("/probe", name="probe_first")
    def probe_first() -> dict[str, str]:
        return {"handler": "first"}

    @probe.get("/probe", name="probe_second")
    def probe_second() -> dict[str, str]:
        return {"handler": "second"}

    with pytest.raises(RouteUniquenessError) as exc_info:
        validate_application_routes(probe)

    message = str(exc_info.value)
    assert "Duplicate HTTP registration: GET /probe" in message
    assert "probe_first" in message
    assert "probe_second" in message
    assert "position=" in message


@pytest.mark.unit
def test_same_path_with_disjoint_methods_is_allowed() -> None:
    probe = FastAPI()

    @probe.get("/resource")
    def read_resource() -> dict[str, str]:
        return {"method": "GET"}

    @probe.post("/resource")
    def write_resource() -> dict[str, str]:
        return {"method": "POST"}

    validate_application_routes(probe)


@pytest.mark.unit
def test_automatic_head_registration_does_not_produce_false_positives() -> None:
    probe = FastAPI()

    @probe.api_route("/head-safe", methods=["GET", "HEAD"])
    def head_safe() -> dict[str, str]:
        return {"ok": True}

    validate_application_routes(probe)

    entries = collect_route_registrations(probe.routes)
    head_safe = [entry for entry in entries if entry.path == "/head-safe"]
    assert len(head_safe) == 1
    assert head_safe[0].methods == frozenset({"GET", "HEAD"})


@pytest.mark.unit
def test_mounted_router_duplicate_and_duplicate_route_name_are_detected() -> None:
    child = APIRouter()

    @child.get("/child", name="shared_child_name")
    def child_get() -> dict[str, str]:
        return {"child": "get"}

    duplicate_mount = FastAPI()
    duplicate_mount.mount("/shared", child)
    duplicate_mount.mount("/shared", child)

    with pytest.raises(RouteUniquenessError) as mount_exc:
        validate_application_routes(duplicate_mount)
    mount_message = str(mount_exc.value)
    assert "Duplicate mount path: /shared" in mount_message

    name_probe = FastAPI()

    @name_probe.get("/alpha", name="shared_name")
    def alpha() -> dict[str, str]:
        return {"route": "alpha"}

    @name_probe.get("/beta", name="shared_name")
    def beta() -> dict[str, str]:
        return {"route": "beta"}

    with pytest.raises(RouteUniquenessError) as name_exc:
        validate_application_routes(name_probe)
    name_message = str(name_exc.value)
    assert "Duplicate route name used by different endpoints: 'shared_name'" in name_message
    assert "/alpha" in name_message
    assert "/beta" in name_message


@pytest.mark.unit
def test_openapi_operation_ids_are_unique_and_deterministic() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        validate_application_routes(app)
        assert_openapi_operation_ids_deterministic(app)


@pytest.mark.unit
def test_mutation_adding_second_audit_route_fails_guard() -> None:
    mutant = FastAPI()
    mutant_router = APIRouter(prefix="/admin", tags=["admin"])

    @mutant_router.get("/audit", response_class=HTMLResponse, name="audit_a")
    def audit_a() -> HTMLResponse:
        return HTMLResponse("a")

    @mutant_router.get("/audit", response_class=HTMLResponse, name="audit_b")
    def audit_b() -> HTMLResponse:
        return HTMLResponse("b")

    mutant.include_router(mutant_router)

    with pytest.raises(RouteUniquenessError) as exc_info:
        validate_application_routes(mutant)

    message = str(exc_info.value)
    assert "Duplicate HTTP registration: GET /admin/audit" in message
    assert "audit_a" in message
    assert "audit_b" in message


@pytest.mark.unit
def test_collect_route_registrations_includes_mounts_and_skips_websocket_false_positives() -> None:
    probe = FastAPI()
    static = FastAPI()

    @static.get("/file")
    def static_file() -> dict[str, str]:
        return {"file": True}

    probe.mount("/static", static)

    @probe.websocket("/ws")
    async def ws_endpoint(websocket) -> None:  # type: ignore[no-untyped-def]
        await websocket.accept()

    registrations = collect_route_registrations(probe.routes)
    kinds = {entry.kind for entry in registrations}
    assert "mount" in kinds
    assert "websocket" in kinds
    assert "http" in kinds

    validate_application_routes(probe)


@pytest.mark.unit
def test_included_router_duplicate_is_detected() -> None:
    probe = FastAPI()
    router = APIRouter()

    @router.get("/dup")
    def first() -> dict[str, str]:
        return {"n": 1}

    @router.get("/dup")
    def second() -> dict[str, str]:
        return {"n": 2}

    probe.include_router(router)

    with pytest.raises(RouteUniquenessError):
        validate_application_routes(probe)
