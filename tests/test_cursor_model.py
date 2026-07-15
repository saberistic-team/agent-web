"""Unit tests for shared Cursor SDK model selection (Sonnet + Max Mode)."""

from __future__ import annotations

import pytest

from cursor_model import (
    DEFAULT_CURSOR_MODEL,
    cursor_max_mode_enabled,
    cursor_model_dict,
    cursor_model_id,
)


@pytest.mark.unit
def test_default_model_is_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_MODEL", raising=False)
    assert DEFAULT_CURSOR_MODEL == "sonnet-4.5"
    assert cursor_model_id() == "sonnet-4.5"


@pytest.mark.unit
def test_cursor_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_MODEL", "opus-4.5")
    assert cursor_model_id() == "opus-4.5"


@pytest.mark.unit
def test_max_mode_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_MAX_MODE", raising=False)
    assert cursor_max_mode_enabled() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["false", "0", "no", "off", "False"])
def test_max_mode_can_be_disabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CURSOR_MAX_MODE", value)
    assert cursor_max_mode_enabled() is False


@pytest.mark.unit
def test_cursor_model_dict_includes_max_mode_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_MODEL", raising=False)
    monkeypatch.delenv("CURSOR_MAX_MODE", raising=False)
    payload = cursor_model_dict()
    assert payload["id"] == "sonnet-4.5"
    assert {"id": "max_mode", "value": "true"} in payload["params"]


@pytest.mark.unit
def test_cursor_model_dict_without_max_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_MAX_MODE", "false")
    payload = cursor_model_dict("sonnet-4.5")
    assert payload == {"id": "sonnet-4.5"}


@pytest.mark.unit
def test_cursor_model_selection_builds_sdk_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cursor-sdk lives in requirements-agents.txt (kept out of the lean
    # Render/hello-API requirements.txt); skip where it isn't installed.
    ModelSelection = pytest.importorskip("cursor_sdk").ModelSelection
    from cursor_model import cursor_model_selection

    monkeypatch.delenv("CURSOR_MODEL", raising=False)
    monkeypatch.delenv("CURSOR_MAX_MODE", raising=False)
    selection = cursor_model_selection()
    assert isinstance(selection, ModelSelection)
    assert selection.id == "sonnet-4.5"
    assert any(p.id == "max_mode" and p.value == "true" for p in selection.params)
