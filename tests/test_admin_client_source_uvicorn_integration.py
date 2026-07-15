"""Uvicorn integration coverage for verified admin client-source resolution."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin_client_source import reset_client_source_telemetry_for_tests, resolve_admin_login_client_source
from app.asgi import ImmediatePeerMiddleware
from app.config import get_settings

REAL_CLIENT = "203.0.113.77"
SPOOFED_LEFTMOST = "203.0.113.99"
CLOUDFLARE_HOP = "172.18.0.1"
TRUSTED_PROXY_IPS = "10.0.0.0/8,172.16.0.0/12,127.0.0.0/8"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_probe_app(captured: dict[str, str]) -> ImmediatePeerMiddleware:
    probe = FastAPI()

    @probe.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @probe.middleware("http")
    async def capture_resolution(request: Request, call_next):  # noqa: ANN001
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        captured["source"] = resolution.source
        captured["path"] = resolution.path
        captured["immediate_peer"] = str(request.scope.get("immediate_peer", ""))
        return await call_next(request)

    stack = ProxyHeadersMiddleware(probe, trusted_hosts=TRUSTED_PROXY_IPS)
    return ImmediatePeerMiddleware(stack)


def _start_uvicorn_probe() -> tuple[Any, threading.Thread, int, dict[str, str]]:
    captured: dict[str, str] = {}
    port = _free_port()
    config = uvicorn.Config(
        _build_probe_app(captured),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=False,
    )
    server = uvicorn.Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    reset_client_source_telemetry_for_tests()
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("uvicorn server failed to start")
    return server, thread, port, captured


def _stop_uvicorn(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.integration
def test_uvicorn_proxy_stack_resolves_client_from_trusted_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same ImmediatePeer → ProxyHeaders stack used in deployment."""
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", TRUSTED_PROXY_IPS)

    server, thread, port, captured = _start_uvicorn_probe()
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            headers={
                "X-Forwarded-For": f"{SPOOFED_LEFTMOST}, {REAL_CLIENT}, {CLOUDFLARE_HOP}",
            },
            timeout=5.0,
        )
        assert response.status_code == 200
        assert captured["immediate_peer"].startswith("127.0.0.1")
        assert captured["source"] == REAL_CLIENT
        assert captured["path"] == "verified_forwarded_chain"
    finally:
        _stop_uvicorn(server, thread)


@pytest.mark.integration
def test_uvicorn_direct_peer_ignores_spoofed_xff_when_peer_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_IPS", "10.0.0.0/8")

    captured: dict[str, str] = {}
    probe = FastAPI()

    @probe.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @probe.middleware("http")
    async def capture_resolution(request: Request, call_next):  # noqa: ANN001
        settings = get_settings()
        resolution = resolve_admin_login_client_source(request, settings)
        captured["source"] = resolution.source
        captured["path"] = resolution.path
        captured["immediate_peer"] = str(request.scope.get("immediate_peer", ""))
        return await call_next(request)

    stack = ImmediatePeerMiddleware(probe)
    port = _free_port()
    config = uvicorn.Config(
        stack,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        proxy_headers=False,
    )
    server = uvicorn.Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)

    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/health",
            headers={"X-Forwarded-For": SPOOFED_LEFTMOST},
            timeout=5.0,
        )
        assert response.status_code == 200
        assert captured["source"] == captured["immediate_peer"]
        assert captured["path"] == "untrusted_peer_headers_ignored"
        assert captured["source"] != SPOOFED_LEFTMOST
    finally:
        server.should_exit = True
        thread.join(timeout=5)
