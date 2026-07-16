"""Validate runtime route and OpenAPI registrations for uniqueness.

Enforcement point: required unit tests (``tests/test_route_uniqueness.py``). Production
startup may call ``validate_application_routes`` later; tests are the deterministic gate.

Exclusions (not duplicate false positives):
- ``HEAD`` on a route that also declares ``GET`` (Starlette/FastAPI auto-registration).
- ``OPTIONS`` (framework/CORS handlers).
- ``WebSocketRoute`` entries are collected for diagnostics only; HTTP method/path
  uniqueness does not apply.
- ``Mount`` subapplications are checked for duplicate mount paths, not HTTP methods.

Allowlist: ``ROUTE_UNIQUENESS_ALLOWLIST`` names exact method/path or operation-id
collisions with a documented reason. No wildcard suppression.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute

# Exact duplicates only — empty unless a demonstrated framework exception cannot be
# handled in code. Each entry must name method(s), path, and reason.
ROUTE_UNIQUENESS_ALLOWLIST: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class RouteRegistration:
    """One HTTP, mount, or websocket registration in the runtime route tree."""

    kind: str
    methods: frozenset[str]
    path: str
    name: str | None
    endpoint: str
    position: tuple[int, ...]


class RouteUniquenessError(ValueError):
    """Raised when duplicate routes or OpenAPI operation IDs are detected."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def _endpoint_label(endpoint: Any) -> str:
    module = getattr(endpoint, "__module__", "") or "unknown"
    qualname = getattr(endpoint, "__qualname__", repr(endpoint))
    return f"{module}.{qualname}"


def _normalize_path(prefix: str, path: str) -> str:
    combined = f"{prefix}{path}"
    if not combined.startswith("/"):
        combined = f"/{combined}"
    return combined.replace("//", "/")


def _methods_for_duplicate_check(methods: Iterable[str] | None) -> frozenset[str]:
    if not methods:
        return frozenset()
    ignored = {"HEAD", "OPTIONS"}
    return frozenset(m for m in methods if m not in ignored)


def _is_allowlisted(method: str, path: str) -> bool:
    for entry in ROUTE_UNIQUENESS_ALLOWLIST:
        if entry.get("method") == method and entry.get("path") == path:
            return True
    return False


def collect_route_registrations(routes: list[BaseRoute], *, prefix: str = "") -> list[RouteRegistration]:
    """Walk the fully registered runtime route tree."""
    registrations: list[RouteRegistration] = []

    for index, route in enumerate(routes):
        position = (index,)
        if type(route).__name__ == "_IncludedRouter":
            registrations.extend(
                collect_route_registrations(route.original_router.routes, prefix=prefix)
            )
            continue

        if isinstance(route, Mount):
            mount_path = _normalize_path(prefix, route.path)
            registrations.append(
                RouteRegistration(
                    kind="mount",
                    methods=frozenset(),
                    path=mount_path,
                    name=route.name,
                    endpoint=_endpoint_label(route.app),
                    position=position,
                )
            )
            child_prefix = mount_path if mount_path.endswith("/") else f"{mount_path}/"
            registrations.extend(
                collect_route_registrations(route.routes, prefix=child_prefix)
            )
            continue

        if isinstance(route, WebSocketRoute):
            registrations.append(
                RouteRegistration(
                    kind="websocket",
                    methods=frozenset({"WEBSOCKET"}),
                    path=_normalize_path(prefix, route.path),
                    name=route.name,
                    endpoint=_endpoint_label(route.endpoint),
                    position=position,
                )
            )
            continue

        if isinstance(route, (APIRoute, Route)) or (
            hasattr(route, "path") and hasattr(route, "methods")
        ):
            registrations.append(
                RouteRegistration(
                    kind="http",
                    methods=frozenset(route.methods or []),
                    path=_normalize_path(prefix, route.path),
                    name=route.name,
                    endpoint=_endpoint_label(route.endpoint),
                    position=position,
                )
            )
            continue

        if hasattr(route, "routes"):
            registrations.extend(collect_route_registrations(route.routes, prefix=prefix))

    return registrations


def _duplicate_method_path_errors(
    registrations: list[RouteRegistration],
) -> list[str]:
    by_pair: dict[tuple[str, str], list[RouteRegistration]] = defaultdict(list)
    for reg in registrations:
        if reg.kind != "http":
            continue
        for method in _methods_for_duplicate_check(reg.methods):
            if _is_allowlisted(method, reg.path):
                continue
            by_pair[(method, reg.path)].append(reg)

    messages: list[str] = []
    for (method, path), entries in sorted(by_pair.items()):
        if len(entries) < 2:
            continue
        lines = [
            f"Duplicate HTTP registration: {method} {path}",
        ]
        for entry in entries:
            lines.append(
                "  - "
                f"name={entry.name!r} "
                f"endpoint={entry.endpoint} "
                f"position={entry.position} "
                f"methods={sorted(entry.methods)}"
            )
        messages.append("\n".join(lines))
    return messages


def _duplicate_mount_path_errors(registrations: list[RouteRegistration]) -> list[str]:
    by_path: dict[str, list[RouteRegistration]] = defaultdict(list)
    for reg in registrations:
        if reg.kind != "mount":
            continue
        by_path[reg.path].append(reg)

    messages: list[str] = []
    for path, entries in sorted(by_path.items()):
        if len(entries) < 2:
            continue
        lines = [f"Duplicate mount path: {path}"]
        for entry in entries:
            lines.append(
                "  - "
                f"name={entry.name!r} "
                f"endpoint={entry.endpoint} "
                f"position={entry.position}"
            )
        messages.append("\n".join(lines))
    return messages


def _duplicate_route_name_errors(registrations: list[RouteRegistration]) -> list[str]:
    by_name: dict[str, list[RouteRegistration]] = defaultdict(list)
    for reg in registrations:
        if reg.kind != "http" or not reg.name:
            continue
        by_name[reg.name].append(reg)

    messages: list[str] = []
    for name, entries in sorted(by_name.items()):
        endpoints = {entry.endpoint for entry in entries}
        if len(endpoints) < 2:
            continue
        lines = [f"Duplicate route name used by different endpoints: {name!r}"]
        for entry in entries:
            lines.append(
                "  - "
                f"{entry.methods} {entry.path} "
                f"endpoint={entry.endpoint} "
                f"position={entry.position}"
            )
        messages.append("\n".join(lines))
    return messages


def _openapi_operation_entries(app: FastAPI) -> list[tuple[str, str, str]]:
    schema = app.openapi()
    entries: list[tuple[str, str, str]] = []
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            upper = method.upper()
            if upper not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue
            operation_id = operation.get("operationId")
            if operation_id:
                entries.append((operation_id, upper, path))
    return entries


def _duplicate_operation_id_errors(app: FastAPI) -> list[str]:
    entries = _openapi_operation_entries(app)
    by_id: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for operation_id, method, path in entries:
        by_id[operation_id].append((method, path))

    messages: list[str] = []
    for operation_id, collisions in sorted(by_id.items()):
        if len(collisions) < 2:
            continue
        methods = {method for method, _ in collisions}
        paths = {path for _, path in collisions}
        # Framework exception: GET + HEAD on the same path is one logical operation.
        if methods <= {"GET", "HEAD"} and len(paths) == 1:
            continue
        lines = [f"Duplicate OpenAPI operationId: {operation_id!r}"]
        for method, path in sorted(collisions):
            lines.append(f"  - {method} {path}")
        messages.append("\n".join(lines))
    return messages


def validate_application_routes(app: FastAPI) -> None:
    """Raise ``RouteUniquenessError`` when duplicate registrations are detected."""
    registrations = collect_route_registrations(app.routes)
    messages = [
        *_duplicate_method_path_errors(registrations),
        *_duplicate_mount_path_errors(registrations),
        *_duplicate_route_name_errors(registrations),
        *_duplicate_operation_id_errors(app),
    ]
    if messages:
        raise RouteUniquenessError(messages)


def assert_openapi_operation_ids_deterministic(app: FastAPI) -> None:
    """Require repeated OpenAPI generation to yield identical operation IDs."""
    first = app.openapi()
    second = app.openapi()
    assert first is second, "FastAPI should cache OpenAPI schema across calls"

    def _ids(schema: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    operation_id = operation.get("operationId")
                    if operation_id:
                        ids.append(operation_id)
        return ids

    assert _ids(first) == _ids(second)
