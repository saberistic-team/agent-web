"""Permission-aware lead discovery source adapters."""

from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.fetcher import DISCOVERY_USER_AGENT, FetchPolicy, HttpFetcher
from app.discovery.runner import run_adapter
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCandidate,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryObservation,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)

__all__ = [
    "AccessDocumentation",
    "DISCOVERY_USER_AGENT",
    "DiscoveryCandidate",
    "DiscoveryCheckpoint",
    "DiscoveryError",
    "DiscoveryObservation",
    "DiscoveryRunResult",
    "DiscoverySourceRegistry",
    "FetchPolicy",
    "HttpFetcher",
    "RetrievalMethod",
    "SourceIdentity",
    "TermsReviewMetadata",
    "run_adapter",
]
