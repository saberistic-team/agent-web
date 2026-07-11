"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.briefs import router as briefs_router
from app.db import init_db

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
ASSETS_DIR = SITE_DIR / "assets"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="agent-web", version="0.3.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.include_router(briefs_router)


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


@app.get("/request-brief")
def request_brief() -> FileResponse:
    return FileResponse(SITE_DIR / "request-brief.html")


@app.get("/request-success")
def request_success() -> FileResponse:
    return FileResponse(SITE_DIR / "request-success.html")
