"""Minimal ASGI app for uvicorn proxy-trust integration tests."""

from __future__ import annotations

from fastapi import FastAPI, Request

from app.config import get_settings
from app.proxy_trust import resolve_admin_login_client_source

app = FastAPI()


@app.get("/source")
def source_probe(request: Request) -> dict[str, str]:
    resolution = resolve_admin_login_client_source(request, get_settings())
    return {"source": resolution.address, "path": resolution.path.value}
