"""Minimal ASGI app for trusted-proxy integration tests."""

from __future__ import annotations

from fastapi import FastAPI, Request

from app.admin_client_source import resolve_admin_login_client_source
from app.config import get_settings

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/source")
def source(request: Request) -> dict[str, str]:
    resolution = resolve_admin_login_client_source(request, get_settings())
    return {"source": resolution.source, "path": resolution.path}
