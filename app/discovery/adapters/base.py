"""Base adapter helpers shared by concrete discovery sources."""

from __future__ import annotations

from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.fetcher import HttpFetcher
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryRunResult,
    SourceIdentity,
    TermsReviewMetadata,
)


class BaseDiscoveryAdapter:
    """Shared adapter behavior — returns candidates only, never CRM writes."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
    ) -> None:
        self._identity = identity
        self._terms = terms
        self._access = access

    @property
    def identity(self) -> SourceIdentity:
        return self._identity

    @property
    def terms(self) -> TermsReviewMetadata:
        return self._terms

    @property
    def access(self) -> AccessDocumentation:
        return self._access

    @property
    def is_operational(self) -> bool:
        if self._terms.robots_allowed is False:
            return False
        return self._access.is_complete

    def blocked_result(self) -> DiscoveryRunResult:
        return DiscoveryRunResult(
            source_id=self._identity.source_id,
            errors=[
                DiscoveryError(
                    code="source_blocked",
                    message=(
                        "Source is blocked until access method and operational "
                        "limits are documented and robots review permits access"
                    ),
                    recoverable=False,
                )
            ],
        )

    def discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> DiscoveryRunResult:
        if not self.is_operational:
            return self.blocked_result()
        return self._discover(checkpoint=checkpoint, fetcher=fetcher)

    def _discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None,
        fetcher: HttpFetcher | None,
    ) -> DiscoveryRunResult:
        raise NotImplementedError


def assert_adapter_contract(adapter: DiscoverySourceAdapter) -> None:
    """Validate adapter exposes required contract fields."""
    assert adapter.identity.source_id
    assert adapter.identity.display_name
    assert adapter.identity.source_kind
    assert adapter.access.retrieval_method
    assert adapter.access.user_agent
