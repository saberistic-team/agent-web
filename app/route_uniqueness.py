"""Validate registered HTTP routes and OpenAPI operation IDs for uniqueness.

Enforcement runs in CI via ``tests/test_route_uniqueness.py``. Production startup
may call ``validate_app_routes`` when deterministic duplicate detection is needed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute

# FastAPI may assign one operationId to both GET and HEAD on a single api_route
# registration. That pairing is excluded in ``find_openapi_operation_id_duplicates``.
OPENAPI_GET_HEAD_SAME_PATH_REASON = (
    "GET and HEAD on the same path share one api_route registration; "
    "FastAPI may emit the same operationId for both methods."
)


@dataclass(frozen=True)
class RouteRegistration:
    method: str
    path: str
    route_name: str | None
    endpoint: str
    position: int
    route_key: int

    def describe(self) -> str:
        name = self.route_name or "<unnamed>"
        return (
            f"position={self.position} method={self.method} path={self.path!r} "
            f"name={name!r} endpoint={self.endpoint}"
        )


class RouteUniquenessError(ValueError):
    """Raised when duplicate routes or OpenAPI operation IDs are detected."""


def _normalize_path(prefix: str, path: str) -> str:
    combined = f"{prefix.rstrip('/')}/{path.lstrip('/')}".replace("//", "/")
    if combined == "":
        return "/"
    return combined


def _endpoint_label(endpoint: Any) -> str:
    module = getattr(endpoint, "__module__", "?")
    qualname = getattr(endpoint, "__qualname__", repr(endpoint))
    return f"{module}.{qualname}"


def _iter_routes(app: FastAPI) -> Iterable[tuple[BaseRoute, str, int]]:
    position = 0
    for route in app.routes:
        yield route, "", position
        position += 1


def _included_router_prefix(route: BaseRoute) -> str | None:
    include_context = getattr(route, "include_context", None)
    if include_context is None:
        return None
    return include_context.prefix or ""


def _included_router_routes(route: BaseRoute) -> list[BaseRoute] | None:
    original_router = getattr(route, "original_router", None)
    if original_router is None:
        return None
    return list(original_router.routes)


def iter_route_registrations(app: FastAPI) -> list[RouteRegistration]:
    """Collect HTTP route registrations from the runtime app tree.

    Exclusions (documented):
    - ``Mount`` subapplications (e.g. ``/assets`` StaticFiles) are not expanded;
      their internal catch-all routes are framework-owned and do not participate in
      application handler duplicate detection.
    - ``WebSocketRoute`` entries are skipped; this app does not register websockets.
    """
    registrations: list[RouteRegistration] = []
    position = 0

    def add_route(
        route: BaseRoute,
        *,
        prefix: str,
        source_position: int,
        child_index: int | None = None,
    ) -> None:
        nonlocal position
        if isinstance(route, WebSocketRoute):
            return

        if isinstance(route, Mount):
            return

        included_prefix = _included_router_prefix(route)
        included_routes = _included_router_routes(route)
        if included_prefix is not None and included_routes is not None:
            for child_idx, child in enumerate(included_routes):
                add_route(
                    child,
                    prefix=included_prefix,
                    source_position=source_position,
                    child_index=child_idx,
                )
            return

        methods: set[str]
        path: str
        route_name: str | None
        endpoint: Any
        route_key: int

        if isinstance(route, APIRoute):
            methods = set(route.methods)
            path = _normalize_path(prefix, route.path)
            route_name = route.name
            endpoint = route.endpoint
            route_key = id(route)
        elif isinstance(route, Route):
            methods = set(route.methods)
            path = _normalize_path(prefix, route.path)
            route_name = route.name
            endpoint = route.endpoint
            route_key = id(route)
        else:
            return

        reg_position = position
        position += 1
        endpoint_label = _endpoint_label(endpoint)
        for method in sorted(methods):
            registrations.append(
                RouteRegistration(
                    method=method,
                    path=path,
                    route_name=route_name,
                    endpoint=endpoint_label,
                    position=reg_position,
                    route_key=route_key,
                )
            )

    for route, _prefix, source_position in _iter_routes(app):
        add_route(route, prefix="", source_position=source_position)

    return registrations


def _authoritative_methods(methods: Iterable[str]) -> set[str]:
    """Drop framework-companion methods that should not create false duplicates."""
    normalized = set(methods)
    if "GET" in normalized:
        normalized.discard("HEAD")
    return normalized


def find_method_path_duplicates(
    registrations: list[RouteRegistration],
) -> dict[tuple[str, str], list[RouteRegistration]]:
    grouped: dict[tuple[str, str], list[RouteRegistration]] = defaultdict(list)
    for registration in registrations:
        for method in _authoritative_methods({registration.method}):
            grouped[(method, registration.path)].append(registration)

    return {key: entries for key, entries in grouped.items() if len(entries) > 1}


def find_route_name_collisions(
    registrations: list[RouteRegistration],
) -> dict[str, list[RouteRegistration]]:
    """Return route names mapped to registrations with differing endpoint objects."""
    by_name: dict[str, list[RouteRegistration]] = defaultdict(list)
    for registration in registrations:
        if registration.route_name:
            by_name[registration.route_name].append(registration)

    collisions: dict[str, list[RouteRegistration]] = {}
    for name, entries in by_name.items():
        endpoint_ids = {entry.route_key for entry in entries}
        if len(endpoint_ids) > 1:
            collisions[name] = entries
    return collisions


def format_method_path_duplicate_error(
    duplicates: dict[tuple[str, str], list[RouteRegistration]],
) -> str:
    lines = ["Duplicate HTTP method/path route registrations detected:"]
    for (method, path), entries in sorted(duplicates.items()):
        lines.append(f"- {method} {path}")
        for entry in entries:
            lines.append(f"  - {entry.describe()}")
    return "\n".join(lines)


def format_route_name_collision_error(
    collisions: dict[str, list[RouteRegistration]],
) -> str:
    lines = ["Duplicate route names bound to different endpoints:"]
    for name, entries in sorted(collisions.items()):
        lines.append(f"- name={name!r}")
        for entry in entries:
            lines.append(f"  - {entry.describe()}")
    return "\n".join(lines)


def validate_method_path_uniqueness(app: FastAPI) -> None:
    registrations = iter_route_registrations(app)
    duplicates = find_method_path_duplicates(registrations)
    if duplicates:
        raise RouteUniquenessError(format_method_path_duplicate_error(duplicates))


def collect_openapi_operation_ids(app: FastAPI) -> list[tuple[str, str, str]]:
    schema = app.openapi()
    collected: list[tuple[str, str, str]] = []
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            operation_id = operation.get("operationId")
            if operation_id:
                collected.append((operation_id, method.upper(), path))
    return collected


def _is_framework_get_head_operation_duplicate(
    entries: list[tuple[str, str, str]],
) -> bool:
    if len(entries) != 2:
        return False
    methods = {method for _oid, method, _path in entries}
    paths = {path for _oid, _method, path in entries}
    return methods.issubset({"GET", "HEAD"}) and len(paths) == 1


def find_openapi_operation_id_duplicates(
    app: FastAPI,
) -> dict[str, list[tuple[str, str, str]]]:
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for operation_id, method, path in collect_openapi_operation_ids(app):
        grouped[operation_id].append((operation_id, method, path))

    duplicates: dict[str, list[tuple[str, str, str]]] = {}
    for operation_id, entries in grouped.items():
        if len(entries) <= 1:
            continue
        if _is_framework_get_head_operation_duplicate(entries):
            continue
        duplicates[operation_id] = entries
    return duplicates


def format_openapi_duplicate_error(
    duplicates: dict[str, list[tuple[str, str, str]]],
) -> str:
    lines = ["Duplicate OpenAPI operationId values detected:"]
    for operation_id, entries in sorted(duplicates.items()):
        lines.append(f"- operationId={operation_id!r}")
        for _oid, method, path in entries:
            lines.append(f"  - {method} {path}")
    return "\n".join(lines)


def validate_openapi_operation_ids(app: FastAPI) -> None:
    duplicates = find_openapi_operation_id_duplicates(app)
    if duplicates:
        raise RouteUniquenessError(format_openapi_duplicate_error(duplicates))


def validate_app_routes(app: FastAPI) -> None:
    """Validate runtime route method/path pairs and OpenAPI operation IDs."""
    validate_method_path_uniqueness(app)
    validate_openapi_operation_ids(app)
