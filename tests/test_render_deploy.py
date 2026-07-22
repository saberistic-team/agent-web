"""Unit tests for Render deploy trigger + wait."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from render_deploy import (
    RenderDeployError,
    find_deploy_for_commit,
    resolve_deploy_id,
    resolve_service_id,
    service_id_from_hook,
    trigger_deploy,
    verify_health,
    wait_for_deploy,
)


@pytest.mark.unit
def test_service_id_from_hook() -> None:
    url = "https://api.render.com/deploy/srv-abc123?key=secret"
    assert service_id_from_hook(url) == "srv-abc123"
    assert resolve_service_id(url) == "srv-abc123"


@pytest.mark.unit
def test_trigger_deploy_appends_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_http(url: str, **kwargs):  # noqa: ANN003
        seen["url"] = url
        return 200, {"deployId": "dep-1"}

    monkeypatch.setattr("render_deploy._http_json", fake_http)
    result = trigger_deploy(
        "https://api.render.com/deploy/srv-abc?key=k",
        ref="abcdef0123456789",
    )
    assert result["deploy_id"] == "dep-1"
    assert "ref=abcdef0123456789" in seen["url"]


@pytest.mark.unit
def test_wait_for_deploy_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter(
        [
            {"id": "dep-1", "status": "build_in_progress"},
            {"id": "dep-1", "status": "live"},
        ]
    )
    monkeypatch.setattr("render_deploy.fetch_deploy", lambda *a, **k: next(states))
    monkeypatch.setattr("render_deploy.time.sleep", lambda _s: None)
    deploy = wait_for_deploy("key", "srv-1", "dep-1", poll_seconds=0)
    assert deploy["status"] == "live"


@pytest.mark.unit
def test_wait_for_deploy_fails_on_update_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "render_deploy.fetch_deploy",
        lambda *a, **k: {"id": "dep-1", "status": "update_failed"},
    )
    monkeypatch.setattr("render_deploy.time.sleep", lambda _s: None)
    with pytest.raises(RenderDeployError, match="update_failed"):
        wait_for_deploy("key", "srv-1", "dep-1", poll_seconds=0)


@pytest.mark.unit
def test_resolve_deploy_id_from_list_when_hook_omits_id() -> None:
    with patch(
        "render_deploy.list_deploys",
        return_value=[
            {"id": "dep-9", "commit": {"id": "aaaaaaaa"}},
            {"id": "dep-8", "commit": {"id": "bbbbbbbb"}},
        ],
    ):
        deploy_id = resolve_deploy_id(
            api_key="k",
            service_id="srv-1",
            triggered={"deploy_id": None, "http_status": 202},
            ref="aaaaaaaa",
        )
    assert deploy_id == "dep-9"


@pytest.mark.unit
def test_find_deploy_for_commit_short_sha() -> None:
    deploy = find_deploy_for_commit(
        [{"id": "1", "commit": {"id": "abcdef012345"}}],
        "abcdef0",
    )
    assert deploy is not None
    assert deploy["id"] == "1"


@pytest.mark.unit
def test_verify_health_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "render_deploy._http_json",
        lambda *a, **k: (200, {"status": "ok", "schema_version": "014"}),
    )
    with pytest.raises(RenderDeployError, match="schema_version mismatch"):
        verify_health("https://example.com", expected_schema_version="015")
