"""Tests for concrete production discovery sources (fixture-backed, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.discovery.fetcher import HttpFetcher
from app.discovery.registry_factory import build_production_registry
from app.discovery.runner import run_adapter
from app.discovery.sources import (
    SOURCE_BUILDERS,
    build_crunchbase_news_source,
    build_github_source,
    build_producthunt_source,
    build_techcrunch_funding_source,
)
from app.discovery.types import DiscoveryCheckpoint

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discovery"

_FEED_FIXTURES = {
    "https://www.producthunt.com/feed": "producthunt-atom.xml",
    "https://techcrunch.com/tag/funding/feed/": "techcrunch-funding.xml",
    "https://news.crunchbase.com/feed/": "crunchbase-news.xml",
}


def _fixture_loader(url: str) -> bytes:
    fixture = _FEED_FIXTURES.get(url)
    if fixture is not None:
        return (FIXTURES / fixture).read_bytes()
    if url.startswith("https://api.github.com/search/repositories"):
        return (FIXTURES / "github-search.json").read_bytes()
    raise FileNotFoundError(f"no fixture for {url}")


def _fixture_fetcher() -> HttpFetcher:
    return HttpFetcher(fixture_loader=_fixture_loader)


@pytest.mark.unit
def test_all_source_builders_are_operational() -> None:
    for source_id, builder in SOURCE_BUILDERS.items():
        adapter = builder()
        assert adapter.identity.source_id == source_id
        assert adapter.is_operational, f"{source_id} must pass the documented-access gate"
        assert adapter.access.user_agent
        assert adapter.terms.terms_url


@pytest.mark.unit
@pytest.mark.integration
def test_producthunt_source_produces_launch_candidates() -> None:
    adapter = build_producthunt_source()
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert not result.errors
    names = [candidate.name for candidate in result.candidates]
    assert names == ["Phinq", "apra-fleet"]
    first = result.candidates[0]
    assert first.website == "https://www.producthunt.com/products/phinq"
    assert first.external_id == "producthunt:tag:www.producthunt.com,2005:Post/1221712"
    assert "source:producthunt" in first.signals
    assert "category:ai_infrastructure" in first.signals
    assert first.evidence is not None
    assert first.evidence.snippet == "Stops AI agents before they break something"


@pytest.mark.unit
@pytest.mark.integration
def test_techcrunch_funding_extracts_company_names() -> None:
    adapter = build_techcrunch_funding_source()
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert not result.errors
    names = [candidate.name for candidate in result.candidates]
    assert names[0] == "Serval"
    assert names[1] == "Meridian"
    # Non-funding headline falls back to the full title for operator review.
    assert names[2].startswith("The perfect pitch")
    serval = result.candidates[0]
    assert serval.website == (
        "https://techcrunch.com/2025/10/21/"
        "serval-raises-47-million-to-bring-ai-agent-to-it-service-management/"
    )
    assert "category:ai_infrastructure" in serval.signals
    meridian = result.candidates[1]
    assert "category:fintech" in meridian.signals


@pytest.mark.unit
@pytest.mark.integration
def test_crunchbase_news_strips_exclusive_prefix() -> None:
    adapter = build_crunchbase_news_source()
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert not result.errors
    names = [candidate.name for candidate in result.candidates]
    assert names[0] == "ClearJet"
    assert names[1].startswith("Sector Snapshot")


@pytest.mark.unit
@pytest.mark.integration
def test_github_source_maps_repositories_and_paginates() -> None:
    adapter = build_github_source()
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)
    assert len(result.candidates) == 2

    first = result.candidates[0]
    assert first.name == "ledgerwatch"
    assert first.external_id == "github:990001"
    assert first.website == "https://ledgerwatch.dev"
    assert first.domain == "ledgerwatch.dev"
    assert "language:Go" in first.signals
    assert "topic:fintech" in first.signals
    assert "category:fintech" in first.signals
    assert first.raw_payload is not None
    assert first.raw_payload["owner_login"] == "ledgerwatch"
    assert "owner" not in first.raw_payload

    second = result.candidates[1]
    assert second.website == "https://github.com/inference-grid/inference-grid"

    assert result.checkpoint is not None
    assert result.checkpoint.cursor == "2"  # 100 of 150 fetched → next page
    wrapped = run_adapter(
        adapter,
        checkpoint=DiscoveryCheckpoint(cursor=result.checkpoint.cursor),
        fetcher=_fixture_fetcher(),
    )
    assert wrapped.checkpoint is not None
    assert wrapped.checkpoint.cursor == "1"  # 200 >= total_count 150 → wrap


@pytest.mark.unit
@pytest.mark.integration
def test_registry_factory_enables_all_documented_sources() -> None:
    registry = build_production_registry(
        ["ycombinator", "producthunt", "techcrunch-funding", "crunchbase-news", "github"]
    )
    for source_id in (
        "ycombinator",
        "producthunt",
        "techcrunch-funding",
        "crunchbase-news",
        "github",
    ):
        assert registry.is_enabled(source_id)
        assert registry.get(source_id) is not None
