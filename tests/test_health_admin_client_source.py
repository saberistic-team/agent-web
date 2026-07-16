"""Health endpoint tests for admin client source deployment policy."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.unit
def test_health_reports_admin_client_source_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("ADMIN_TRUSTED_EDGE_CIDRS", raising=False)
    response = client.get("/health")
    assert response.status_code == 200
    policy = response.json()["admin_client_source_policy"]
    assert policy["mode"] == "direct_peer_only"
    assert policy["trusted_proxy_network_count"] == 0


@pytest.mark.unit
def test_health_reports_trusted_proxy_mode_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("ADMIN_TRUSTED_EDGE_CIDRS", "172.64.0.0/13")
    response = client.get("/health")
    policy = response.json()["admin_client_source_policy"]
    assert policy["mode"] == "trusted_proxy_cidrs"
    assert policy["trusted_proxy_network_count"] == 1
    assert policy["trusted_edge_network_count"] == 1
