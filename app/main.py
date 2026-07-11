"""Minimal hello-world HTTP API."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="agent-web hello", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}
