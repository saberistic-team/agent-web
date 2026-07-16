"""Registry for independently enabled discovery source adapters."""

from __future__ import annotations

from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.fetcher import HttpFetcher
from app.discovery.runner import run_adapter
from app.discovery.types import DiscoveryCheckpoint, DiscoveryRunResult


class DiscoverySourceRegistry:
    """Register adapters and run only those explicitly enabled."""

    def __init__(self) -> None:
        self._adapters: dict[str, DiscoverySourceAdapter] = {}
        self._enabled: set[str] = set()

    def register(self, adapter: DiscoverySourceAdapter) -> None:
        source_id = adapter.identity.source_id
        self._adapters[source_id] = adapter

    def unregister(self, source_id: str) -> None:
        self._adapters.pop(source_id, None)
        self._enabled.discard(source_id)

    def get(self, source_id: str) -> DiscoverySourceAdapter | None:
        return self._adapters.get(source_id)

    def list_all(self) -> list[DiscoverySourceAdapter]:
        return list(self._adapters.values())

    def is_enabled(self, source_id: str) -> bool:
        return source_id in self._enabled

    def enable(self, source_id: str) -> None:
        if source_id not in self._adapters:
            raise KeyError(f"unknown discovery source: {source_id}")
        self._enabled.add(source_id)

    def disable(self, source_id: str) -> None:
        self._enabled.discard(source_id)

    def run_enabled(
        self,
        *,
        checkpoints: dict[str, DiscoveryCheckpoint] | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> list[DiscoveryRunResult]:
        results: list[DiscoveryRunResult] = []
        for source_id in sorted(self._enabled):
            adapter = self._adapters[source_id]
            checkpoint = (checkpoints or {}).get(source_id)
            results.append(
                run_adapter(
                    adapter,
                    checkpoint=checkpoint,
                    fetcher=fetcher,
                )
            )
        return results
