"""Unit tests for the Y Combinator discovery adapter (#119)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import httpx
import pytest

from app.discovery.adapters import DiscoverySourceRegistry, build_yc_adapter
from app.discovery.adapters.base import assert_adapter_contract
from app.discovery.adapters.yc import (
    YC_ALGOLIA_QUERY_URL,
    normalize_yc_company,
    parse_algolia_response,
)
from app.discovery.category import crm_category_for_discovery, map_suggested_category
from app.discovery.runner import run_adapter
from app.discovery.types import DiscoveryCheckpoint, RetrievalMethod

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discovery"


def _yc_query_loader(page: int) -> bytes:
    mapping = {
        0: FIXTURES / "yc-algolia-page0.json",
        1: FIXTURES / "yc-algolia-page1-drift.json",
    }
    path = mapping.get(page)
    if path is None:
        raise FileNotFoundError(f"missing fixture for page {page}")
    return path.read_bytes()


def _build_fixture_adapter(**kwargs):
    return build_yc_adapter(query_loader=_yc_query_loader, **kwargs)


@pytest.mark.unit
def test_map_suggested_category_rules_are_transparent() -> None:
    assert (
        map_suggested_category(
            tags=["Fintech", "Payments"],
            description="Banking APIs",
        )
        == "fintech"
    )
    assert (
        map_suggested_category(
            tags=["AI"],
            description="LLM infrastructure for model serving",
        )
        == "ai_infrastructure"
    )
    assert (
        map_suggested_category(
            tags=["Crypto", "Blockchain"],
            description="Digital asset custody",
        )
        == "digital_assets"
    )
    assert map_suggested_category(tags=["Retail", "Marketplace"]) == "unclear"
    assert crm_category_for_discovery("unclear") == "other"


@pytest.mark.unit
def test_parse_algolia_response_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="missing hits"):
        parse_algolia_response(b'{"nbPages": 1}')
    with pytest.raises(ValueError, match="JSON object"):
        parse_algolia_response(b"[]")


@pytest.mark.unit
def test_normalize_yc_company_preserves_provenance_and_category() -> None:
    row = json.loads((FIXTURES / "yc-algolia-page0.json").read_text())["hits"][0]
    candidate = normalize_yc_company(
        row=row,
        source_id="ycombinator",
        source_url=YC_ALGOLIA_QUERY_URL,
        retrieved_at="2026-07-16T00:00:00+00:00",
    )
    assert candidate.name == "LedgerFlow"
    assert candidate.external_id == "ycombinator:12345"
    assert candidate.website == "https://ledgerflow.example.com"
    assert candidate.raw_payload is not None
    assert candidate.raw_payload["suggested_category"] == "fintech"
    assert "category:fintech" in candidate.signals
    assert "batch:Winter 2024" in candidate.signals
    assert candidate.raw_payload["profile_url"] == (
        "https://www.ycombinator.com/companies/ledgerflow"
    )
    assert candidate.evidence is not None
    values = {obs.value for obs in candidate.evidence.observations}
    assert "name=LedgerFlow" in values
    assert "batch=Winter 2024" in values
    assert "location=New York, NY, USA" in values
    assert "suggested_category=fintech" in values
    assert all("stage=" not in value for value in values)


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_normalizes_fixture_page_with_partial_failure() -> None:
    adapter = _build_fixture_adapter()
    result = run_adapter(adapter)
    assert len(result.candidates) == 2
    names = {candidate.name for candidate in result.candidates}
    assert names == {"LedgerFlow", "VectorServe"}
    assert result.candidates[0].raw_payload is not None
    assert result.candidates[0].raw_payload["suggested_category"] == "fintech"
    assert result.candidates[1].raw_payload is not None
    assert result.candidates[1].raw_payload["suggested_category"] == "ai_infrastructure"
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)
    assert result.checkpoint is not None
    assert result.checkpoint.cursor == "1"


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_supports_incremental_page_checkpoint() -> None:
    adapter = _build_fixture_adapter()
    checkpoint = DiscoveryCheckpoint(cursor="1")
    result = run_adapter(adapter, checkpoint=checkpoint)
    assert len(result.candidates) == 1
    assert result.candidates[0].name == "ChainVault"
    assert result.candidates[0].raw_payload is not None
    assert result.candidates[0].raw_payload["suggested_category"] == "digital_assets"
    assert result.checkpoint is not None
    assert result.checkpoint.cursor == "0"


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_tolerates_source_format_drift() -> None:
    adapter = _build_fixture_adapter()
    result = run_adapter(adapter, checkpoint=DiscoveryCheckpoint(cursor="1"))
    candidate = result.candidates[0]
    assert candidate.name == "ChainVault"
    assert candidate.raw_payload is not None
    assert candidate.raw_payload["location"] == "Austin, TX, USA"
    assert "tag:Crypto" in candidate.signals


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_maps_unclear_category() -> None:
    def loader(_page: int) -> bytes:
        return (FIXTURES / "yc-algolia-unclear.json").read_bytes()

    adapter = build_yc_adapter(query_loader=loader)
    result = run_adapter(adapter)
    assert len(result.candidates) == 1
    assert result.candidates[0].raw_payload is not None
    assert result.candidates[0].raw_payload["suggested_category"] == "unclear"
    assert "category:unclear" in result.candidates[0].signals


@pytest.mark.unit
def test_yc_adapter_is_operational_when_documented() -> None:
    adapter = build_yc_adapter(documented=True)
    assert adapter.is_operational is True
    assert_adapter_contract(adapter)
    assert adapter.access.retrieval_method == RetrievalMethod.API
    assert adapter.identity.source_id == "ycombinator"


@pytest.mark.unit
def test_yc_adapter_blocked_until_access_documented() -> None:
    adapter = build_yc_adapter(documented=False)
    assert adapter.is_operational is False
    result = run_adapter(adapter)
    assert result.errors[0].code == "source_blocked"


@pytest.mark.unit
def test_yc_adapter_reports_fetch_failures() -> None:
    def broken_loader(_page: int) -> bytes:
        raise OSError("network down")

    adapter = build_yc_adapter(query_loader=broken_loader)
    result = run_adapter(adapter)
    assert result.partial_failure is True
    assert result.errors[0].code == "fetch_failed"


@pytest.mark.unit
def test_yc_adapter_reports_parse_failures() -> None:
    adapter = build_yc_adapter(query_loader=lambda _page: b"not-json")
    result = run_adapter(adapter)
    assert result.errors[0].code == "parse_failed"


@pytest.mark.unit
def test_yc_adapter_reports_algolia_http_failures() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=_request)

    adapter = build_yc_adapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = run_adapter(adapter)
    assert result.errors[0].code == "fetch_failed"
    assert "403" in result.errors[0].message


@pytest.mark.unit
def test_yc_adapter_module_does_not_import_crm_repositories() -> None:
    import app.discovery.adapters.yc as yc_mod

    source = inspect.getsource(yc_mod)
    assert "crm_service" not in source
    assert "repositories" not in source


@pytest.mark.unit
def test_normalize_yc_company_truncates_long_description_and_id_profile_url() -> None:
    candidate = normalize_yc_company(
        row={
            "id": 42,
            "name": "DeepTech",
            "long_description": "x" * 400,
        },
        source_id="ycombinator",
        source_url=YC_ALGOLIA_QUERY_URL,
        retrieved_at="2026-07-16T00:00:00+00:00",
    )
    assert candidate.evidence is not None
    assert candidate.evidence.snippet is not None
    assert len(candidate.evidence.snippet) == 280
    assert candidate.raw_payload is not None
    assert candidate.raw_payload["profile_url"] == "https://www.ycombinator.com/companies?id=42"


@pytest.mark.unit
def test_normalize_yc_company_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="company name"):
        normalize_yc_company(
            row={"id": 1, "website": "https://example.com"},
            source_id="ycombinator",
            source_url=YC_ALGOLIA_QUERY_URL,
            retrieved_at="2026-07-16T00:00:00+00:00",
        )


@pytest.mark.unit
def test_parse_algolia_response_requires_nb_pages() -> None:
    with pytest.raises(ValueError, match="nbPages"):
        parse_algolia_response(b'{"hits": []}')


@pytest.mark.unit
def test_crm_category_for_discovery_maps_known_categories() -> None:
    assert crm_category_for_discovery("fintech") == "fintech"
    assert crm_category_for_discovery("unknown-key") == "other"


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_tolerates_invalid_checkpoint_cursor() -> None:
    adapter = _build_fixture_adapter()
    checkpoint = DiscoveryCheckpoint(cursor="not-a-number")
    result = run_adapter(adapter, checkpoint=checkpoint)
    assert len(result.candidates) == 2


@pytest.mark.unit
@pytest.mark.integration
def test_yc_adapter_stops_when_checkpoint_page_is_past_end() -> None:
    def loader(page: int) -> bytes:
        if page >= 2:
            return json.dumps(
                {"hits": [], "nbPages": 2, "page": page, "hitsPerPage": 100}
            ).encode("utf-8")
        return _yc_query_loader(page)

    adapter = build_yc_adapter(query_loader=loader)
    checkpoint = DiscoveryCheckpoint(cursor="2")
    result = run_adapter(adapter, checkpoint=checkpoint)
    assert result.candidates == []
    assert result.checkpoint is not None
    assert result.checkpoint.cursor == "0"


@pytest.mark.unit
@pytest.mark.integration
def test_registry_can_run_enabled_ycombinator_source() -> None:
    registry = DiscoverySourceRegistry()
    registry.register(_build_fixture_adapter())
    registry.enable("ycombinator")
    results = registry.run_enabled()
    assert len(results) == 1
    assert results[0].source_id == "ycombinator"
    assert len(results[0].candidates) == 2
