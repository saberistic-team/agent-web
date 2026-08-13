"""Build the production discovery registry from settings."""

from __future__ import annotations

from app.discovery.adapters import DiscoverySourceRegistry
from app.discovery.sources import SOURCE_ALIASES, SOURCE_BUILDERS


def build_production_registry(enabled_source_ids: list[str]) -> DiscoverySourceRegistry:
    """Register and enable adapters configured for production discovery runs."""
    registry = DiscoverySourceRegistry()
    normalized = {
        SOURCE_ALIASES.get(source_id.strip().lower(), source_id.strip().lower())
        for source_id in enabled_source_ids
        if source_id.strip()
    }
    for source_id in sorted(normalized):
        builder = SOURCE_BUILDERS.get(source_id)
        if builder is None:
            continue
        adapter = builder()
        registry.register(adapter)
        registry.enable(adapter.identity.source_id)
    return registry
