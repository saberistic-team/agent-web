"""Tests for production discovery registry factory."""

from __future__ import annotations

import pytest

from app.discovery.registry_factory import build_production_registry


@pytest.mark.unit
@pytest.mark.integration
def test_build_production_registry_enables_ycombinator() -> None:
    registry = build_production_registry(["ycombinator"])
    assert registry.is_enabled("ycombinator")
    assert registry.get("ycombinator") is not None


@pytest.mark.unit
@pytest.mark.integration
def test_build_production_registry_accepts_yc_alias() -> None:
    registry = build_production_registry(["yc"])
    assert registry.is_enabled("ycombinator")


@pytest.mark.unit
@pytest.mark.integration
def test_build_production_registry_ignores_unknown_sources() -> None:
    registry = build_production_registry(["unknown-source"])
    assert not registry.is_enabled("ycombinator")
    assert registry.get("ycombinator") is None
