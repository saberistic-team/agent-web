"""Build the production discovery source registry from settings."""

from __future__ import annotations

from app.discovery.adapters import DiscoverySourceRegistry, build_yc_adapter


def build_production_registry(enabled_source_ids: list[str]) -> DiscoverySourceRegistry:
    """Register and enable adapters configured for production discovery runs."""
    registry = DiscoverySourceRegistry()
    normalized = {source_id.strip().lower() for source_id in enabled_source_ids if source_id.strip()}
    if "ycombinator" in normalized or "yc" in normalized:
        registry.register(build_yc_adapter(documented=True))
        registry.enable("ycombinator")
    return registry
