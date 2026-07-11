"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
ASSETS_DIR = SITE_DIR / "assets"

app = FastAPI(title="agent-web", version="0.2.0")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(SITE_DIR / "index.html")


@app.get("/about")
def about() -> FileResponse:
    return FileResponse(SITE_DIR / "about.html")
