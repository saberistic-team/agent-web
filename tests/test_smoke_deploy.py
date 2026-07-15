"""Tests for post-deploy smoke verification."""

from __future__ import annotations

import pytest

from smoke_deploy import main


@pytest.mark.unit
def test_smoke_deploy_requires_admin_client_source_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get_json(url: str) -> dict:
        calls.append(url)
        if url.endswith("/health"):
            return {
                "status": "ok",
                "admin_client_source_trust": {"immediate_peer_cidrs_configured": False},
            }
        if url.endswith("/hello"):
            return {"message": "hello world"}
        raise AssertionError(url)

    monkeypatch.setattr("smoke_deploy.get_json", fake_get_json)
    assert main(["--base-url", "https://example.com"]) == 1
    assert any("/health" in url for url in calls)


@pytest.mark.unit
def test_smoke_deploy_passes_when_trust_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_json(url: str) -> dict:
        if url.endswith("/health"):
            return {
                "status": "ok",
                "admin_client_source_trust": {"immediate_peer_cidrs_configured": True},
            }
        if url.endswith("/hello"):
            return {"message": "hello world"}
        raise AssertionError(url)

    monkeypatch.setattr("smoke_deploy.get_json", fake_get_json)
    assert main(["--base-url", "https://example.com"]) == 0
