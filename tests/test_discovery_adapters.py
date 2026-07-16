"""Unit tests for permission-aware lead discovery source adapters (#118)."""

from __future__ import annotations

import inspect
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.discovery.adapters import (
    DiscoverySourceRegistry,
    build_api_adapter,
    build_rss_adapter,
    build_sitemap_adapter,
)
from app.discovery.adapters.base import assert_adapter_contract
from app.discovery.adapters.rss import parse_feed_items
from app.discovery.fetcher import (
    DISCOVERY_USER_AGENT,
    FetchCache,
    FetchCacheEntry,
    FetchError,
    FetchPolicy,
    HttpFetcher,
    enforce_size,
    validate_public_url,
)
from app.discovery.normalize import normalize_candidate, stable_external_id
from app.discovery.observation import build_observation, validate_observation
from app.discovery.rate_limit import RateLimiter
from app.discovery.runner import run_adapter
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryObservation,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discovery"


def _fixture_loader(url: str) -> bytes:
    mapping = {
        "https://feeds.example.com/signals.xml": FIXTURES / "sample-feed.xml",
        "https://api.example.com/companies.json": FIXTURES / "sample-api.json",
        "https://directory.example.com/sitemap.xml": FIXTURES / "sample-sitemap.xml",
    }
    path = mapping.get(url)
    if path is None:
        raise FileNotFoundError(url)
    return path.read_bytes()


def _fixture_fetcher() -> HttpFetcher:
    return HttpFetcher(fixture_loader=_fixture_loader)


@pytest.mark.unit
def test_observation_records_provenance_fields() -> None:
    observation = build_observation(
        source_url="https://example.com/signal/1",
        raw_source_id="signal-001",
        value="company=Acme",
        confidence=0.8,
        retrieved_at="2026-01-15T12:00:00+00:00",
        review_at="2026-02-14T12:00:00+00:00",
        expires_at="2026-04-15T12:00:00+00:00",
    )
    assert observation.source_url == "https://example.com/signal/1"
    assert observation.retrieved_at == "2026-01-15T12:00:00+00:00"
    assert observation.raw_source_id == "signal-001"
    assert observation.value == "company=Acme"
    assert observation.confidence == 0.8
    assert observation.review_at == "2026-02-14T12:00:00+00:00"
    assert observation.expires_at == "2026-04-15T12:00:00+00:00"


@pytest.mark.unit
def test_observation_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        validate_observation(
            DiscoveryObservation(
                source_url="https://example.com",
                retrieved_at="2026-01-01T00:00:00+00:00",
                raw_source_id="id-1",
                value="company=Acme",
                confidence=1.5,
                review_at=None,
                expires_at=None,
            )
        )


@pytest.mark.unit
@pytest.mark.integration
def test_normalize_candidate_is_stable_and_domain_aware() -> None:
    first = normalize_candidate(
        source_id="demo_feed",
        name="  Nimbus Analytics ",
        domain="https://www.nimbus.example.com",
        website="nimbus.example.com",
        signals=[" hiring ", "hiring"],
    )
    second = normalize_candidate(
        source_id="demo_feed",
        name="Nimbus Analytics",
        domain="nimbus.example.com",
        website="https://nimbus.example.com",
        signals=["hiring"],
    )
    assert first.name == "Nimbus Analytics"
    assert first.domain == "nimbus.example.com"
    assert first.signals == ("hiring",)
    assert first.external_id == second.external_id


@pytest.mark.unit
def test_stable_external_id_is_deterministic() -> None:
    payload = {"name": "Acme", "domain": "acme.example.com"}
    assert stable_external_id(source_id="src", identity=payload) == stable_external_id(
        source_id="src",
        identity={"domain": "acme.example.com", "name": "Acme"},
    )


@pytest.mark.unit
def test_source_blocked_until_access_documented() -> None:
    adapter = build_rss_adapter(
        source_id="blocked_feed",
        feed_url="https://feeds.example.com/signals.xml",
        documented=False,
    )
    assert adapter.is_operational is False
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert result.candidates == []
    assert result.errors[0].code == "source_blocked"


@pytest.mark.unit
def test_source_blocked_when_robots_disallowed() -> None:
    adapter = build_rss_adapter(
        source_id="robots_blocked",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
        robots_allowed=False,
    )
    assert adapter.is_operational is False
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert result.errors[0].code == "source_blocked"


@pytest.mark.unit
@pytest.mark.integration
def test_rss_adapter_normalizes_candidates_from_fixture() -> None:
    adapter = build_rss_adapter(
        source_id="startup_signals",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
    )
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert len(result.candidates) == 2
    names = {candidate.name for candidate in result.candidates}
    assert names == {"Analytical Engines Ltd", "Vector Research"}
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)
    assert result.checkpoint is not None
    assert result.checkpoint.cursor == "3"


@pytest.mark.unit
@pytest.mark.integration
def test_api_adapter_normalizes_candidates_and_partial_failures() -> None:
    adapter = build_api_adapter(
        source_id="directory_api",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert len(result.candidates) == 2
    assert result.candidates[0].domain == "nimbus.example.com"
    assert result.candidates[0].signals == ("hiring", "product_launch")
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_sitemap_adapter_normalizes_urls_from_fixture() -> None:
    adapter = build_sitemap_adapter(
        source_id="directory_sitemap",
        sitemap_url="https://directory.example.com/sitemap.xml",
        documented=True,
    )
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    assert len(result.candidates) == 2
    assert result.candidates[0].name == "Acme Corp"
    assert result.candidates[0].website == "https://acme-corp.example.com/about"


@pytest.mark.unit
@pytest.mark.integration
def test_registry_runs_only_enabled_sources() -> None:
    registry = DiscoverySourceRegistry()
    enabled = build_rss_adapter(
        source_id="enabled_feed",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
    )
    disabled = build_api_adapter(
        source_id="disabled_api",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    registry.register(enabled)
    registry.register(disabled)
    registry.enable("enabled_feed")

    results = registry.run_enabled(fetcher=_fixture_fetcher())
    assert len(results) == 1
    assert results[0].source_id == "enabled_feed"


@pytest.mark.unit
def test_registry_enable_requires_registration() -> None:
    registry = DiscoverySourceRegistry()
    with pytest.raises(KeyError, match="unknown discovery source"):
        registry.enable("missing")


@pytest.mark.unit
def test_adapter_modules_do_not_import_crm_repositories() -> None:
    import app.discovery.adapters.api as api_mod
    import app.discovery.adapters.rss as rss_mod
    import app.discovery.adapters.sitemap as sitemap_mod

    for module in (api_mod, rss_mod, sitemap_mod):
        source = inspect.getsource(module)
        assert "crm_service" not in source
        assert "repositories" not in source
        assert "companies import" not in source or "normalize_domain" in source


@pytest.mark.unit
def test_fetcher_enforces_response_size_limit() -> None:
    with pytest.raises(FetchError, match="exceeds"):
        enforce_size(b"x" * 600_000, max_bytes=512_000)


@pytest.mark.unit
def test_validate_public_url_blocks_private_resolution() -> None:
    def fake_getaddrinfo(host: str, port: int | None) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]

    with patch("app.discovery.fetcher.socket.getaddrinfo", fake_getaddrinfo):
        with pytest.raises(FetchError, match="private or local"):
            validate_public_url("https://public.example.com/path")


@pytest.mark.unit
def test_rate_limiter_enforces_min_interval() -> None:
    limiter = RateLimiter(requests_per_minute=60)
    assert limiter.would_allow("example.com", now=100.0) is True
    limiter.record_request("example.com", now=100.0)
    assert limiter.would_allow("example.com", now=100.2) is False
    assert limiter.would_allow("example.com", now=101.1) is True


@pytest.mark.unit
def test_http_fetcher_uses_explicit_user_agent_and_timeout() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"companies": []}',
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    fetcher = HttpFetcher(
        policy=FetchPolicy(timeout_seconds=5.0, user_agent=DISCOVERY_USER_AGENT),
        client=client,
        rate_limiter=RateLimiter(requests_per_minute=120),
    )
    result = fetcher.fetch("https://api.example.com/companies.json", skip_dns_validation=True)
    assert result.status_code == 200
    assert captured[0].headers["User-Agent"] == DISCOVERY_USER_AGENT


@pytest.mark.unit
def test_http_fetcher_supports_conditional_not_modified() -> None:
    cache = FetchCache()
    cache.put(
        "https://feeds.example.com/signals.xml",
        FetchCacheEntry(
            etag='W/"cached"',
            last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
            body=b"<rss></rss>",
            content_type="application/xml",
            status_code=200,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == 'W/"cached"'
        return httpx.Response(304, request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    fetcher = HttpFetcher(
        cache=cache,
        client=client,
        rate_limiter=RateLimiter(requests_per_minute=120),
    )
    result = fetcher.fetch(
        "https://feeds.example.com/signals.xml",
        etag='W/"cached"',
        skip_dns_validation=True,
    )
    assert result.not_modified is True
    assert result.body == b"<rss></rss>"


@pytest.mark.unit
def test_rss_checkpoint_skips_already_seen_items() -> None:
    adapter = build_rss_adapter(
        source_id="checkpoint_feed",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
    )
    checkpoint = DiscoveryCheckpoint(cursor="2")
    result = run_adapter(adapter, checkpoint=checkpoint, fetcher=_fixture_fetcher())
    assert len(result.candidates) == 0
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)


@pytest.mark.unit
def test_runner_surfaces_adapter_failures_without_crm_writes() -> None:
    adapter = MagicMock()
    adapter.identity.source_id = "failing"
    adapter.discover.side_effect = RuntimeError("boom")
    result = run_adapter(adapter)
    assert result.partial_failure is True
    assert result.errors[0].code == "adapter_failure"


@pytest.mark.unit
def test_parse_feed_items_supports_rss_and_atom() -> None:
    rss_items = parse_feed_items((FIXTURES / "sample-feed.xml").read_bytes())
    assert len(rss_items) == 3
    atom = b"""<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><id>e1</id><title>Launch</title><company>Atom Corp</company>
      <link>https://atom.example.com</link></entry></feed>"""
    atom_items = parse_feed_items(atom)
    assert atom_items[0]["company"] == "Atom Corp"


@pytest.mark.unit
def test_adapter_contract_fields_present() -> None:
    adapter = build_api_adapter(
        source_id="contract_check",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    assert_adapter_contract(adapter)
    assert adapter.access.retrieval_method == RetrievalMethod.API
    assert adapter.terms.terms_url is not None


@pytest.mark.unit
def test_access_documentation_complete_requires_operational_limits() -> None:
    incomplete = AccessDocumentation(
        retrieval_method=RetrievalMethod.PUBLIC_PAGE,
        user_agent=DISCOVERY_USER_AGENT,
    )
    complete = AccessDocumentation(
        retrieval_method=RetrievalMethod.PUBLIC_PAGE,
        user_agent=DISCOVERY_USER_AGENT,
        documented_at="2026-01-01T00:00:00+00:00",
        rate_limit_requests_per_minute=6,
        max_response_bytes=512_000,
        timeout_seconds=10.0,
    )
    assert incomplete.is_complete is False
    assert complete.is_complete is True


@pytest.mark.unit
@pytest.mark.integration
def test_candidate_evidence_carries_observation_provenance() -> None:
    adapter = build_api_adapter(
        source_id="provenance_api",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    result = run_adapter(adapter, fetcher=_fixture_fetcher())
    candidate = result.candidates[0]
    assert candidate.evidence is not None
    observation = candidate.evidence.observations[0]
    assert observation.source_url == "https://api.example.com/companies.json"
    assert observation.raw_source_id == "co-100"
    assert observation.review_at is not None
    assert observation.expires_at is not None


@pytest.mark.unit
def test_registry_unregister_disable_and_list() -> None:
    registry = DiscoverySourceRegistry()
    adapter = build_api_adapter(
        source_id="temp_api",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    registry.register(adapter)
    registry.enable("temp_api")
    assert registry.is_enabled("temp_api")
    assert len(registry.list_all()) == 1
    registry.disable("temp_api")
    assert registry.is_enabled("temp_api") is False
    registry.unregister("temp_api")
    assert registry.get("temp_api") is None


@pytest.mark.unit
def test_normalize_candidate_rejects_invalid_name_and_website() -> None:
    with pytest.raises(ValueError, match="company name"):
        normalize_candidate(source_id="demo", name="  ")
    with pytest.raises(ValueError, match="website"):
        normalize_candidate(source_id="demo", name="Acme", website="://bad")


@pytest.mark.unit
def test_observation_rejects_empty_source_id_and_url() -> None:
    with pytest.raises(ValueError, match="raw_source_id"):
        validate_observation(
            DiscoveryObservation(
                source_url="https://example.com",
                retrieved_at="2026-01-01T00:00:00+00:00",
                raw_source_id=" ",
                value="company=Acme",
                confidence=0.5,
                review_at=None,
                expires_at=None,
            )
        )


@pytest.mark.unit
def test_fetcher_rejects_disallowed_content_type() -> None:
    from app.discovery.fetcher import enforce_content_type

    with pytest.raises(FetchError, match="not allowed"):
        enforce_content_type("application/octet-stream")


@pytest.mark.unit
def test_fetcher_live_response_caches_successful_body() -> None:
    cache = FetchCache()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "etag": 'W/"fresh"',
                "last-modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            },
            content=b'{"companies": []}',
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = HttpFetcher(
        cache=cache,
        client=client,
        rate_limiter=RateLimiter(requests_per_minute=120),
    )
    fetcher.fetch("https://api.example.com/companies.json", skip_dns_validation=True)
    cached = cache.get("https://api.example.com/companies.json")
    assert cached is not None
    assert cached.etag == 'W/"fresh"'


@pytest.mark.unit
def test_api_adapter_returns_not_modified_checkpoint() -> None:
    adapter = build_api_adapter(
        source_id="cached_api",
        api_url="https://api.example.com/companies.json",
        documented=True,
    )
    cache = FetchCache()
    cache.put(
        "https://api.example.com/companies.json",
        FetchCacheEntry(
            etag='W/"cached"',
            last_modified=None,
            body=b'{"companies": []}',
            content_type="application/json",
            status_code=200,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, request=request)

    fetcher = HttpFetcher(
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(requests_per_minute=120),
    )
    checkpoint = DiscoveryCheckpoint(etag='W/"cached"')
    result = run_adapter(adapter, checkpoint=checkpoint, fetcher=fetcher)
    assert result.candidates == []
    assert result.checkpoint is not None
    assert result.checkpoint.etag == 'W/"cached"'


@pytest.mark.unit
def test_rss_adapter_reports_fetch_failures() -> None:
    adapter = build_rss_adapter(
        source_id="bad_feed",
        feed_url="https://feeds.example.com/missing.xml",
        documented=True,
    )

    def broken_loader(url: str) -> bytes:
        raise FileNotFoundError(url)

    result = run_adapter(
        adapter,
        fetcher=HttpFetcher(fixture_loader=broken_loader),
    )
    assert result.partial_failure is True
    assert result.errors[0].code == "fetch_failed"


@pytest.mark.unit
def test_sitemap_adapter_reports_parse_failures() -> None:
    adapter = build_sitemap_adapter(
        source_id="bad_sitemap",
        sitemap_url="https://directory.example.com/sitemap.xml",
        documented=True,
    )
    fetcher = HttpFetcher(
        fixture_loader=lambda _url: b"<not-a-valid-sitemap",
    )
    result = run_adapter(adapter, fetcher=fetcher)
    assert result.errors[0].code == "parse_failed"


@pytest.mark.unit
def test_rate_limiter_wait_if_needed_blocks() -> None:
    limiter = RateLimiter(requests_per_minute=60)
    with patch("app.discovery.rate_limit.time.monotonic", side_effect=[100.0, 100.5, 100.5]):
        limiter.record_request("example.com")
        with patch("app.discovery.rate_limit.time.sleep") as sleep_mock:
            limiter.wait_if_needed("example.com")
    sleep_mock.assert_called_once()


@pytest.mark.unit
def test_validate_public_url_blocks_localhost() -> None:
    with pytest.raises(FetchError, match="blocked"):
        validate_public_url("https://localhost/admin")


@pytest.mark.unit
def test_rss_adapter_returns_not_modified_checkpoint() -> None:
    adapter = build_rss_adapter(
        source_id="cached_feed",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
    )
    cache = FetchCache()
    cache.put(
        "https://feeds.example.com/signals.xml",
        FetchCacheEntry(
            etag='W/"cached"',
            last_modified=None,
            body=(FIXTURES / "sample-feed.xml").read_bytes(),
            content_type="application/xml",
            status_code=200,
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, request=request)

    fetcher = HttpFetcher(
        cache=cache,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(requests_per_minute=120),
    )
    checkpoint = DiscoveryCheckpoint(etag='W/"cached"')
    result = run_adapter(adapter, checkpoint=checkpoint, fetcher=fetcher)
    assert result.candidates == []
    assert result.checkpoint is not None


@pytest.mark.unit
def test_api_parse_rejects_invalid_payload_shapes() -> None:
    from app.discovery.adapters.api import parse_api_companies

    with pytest.raises(ValueError, match="list or object"):
        parse_api_companies(b"42")
    with pytest.raises(ValueError, match="must be a list"):
        parse_api_companies(b'{"companies": "bad"}')


@pytest.mark.unit
def test_observation_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="value"):
        validate_observation(
            DiscoveryObservation(
                source_url="https://example.com",
                retrieved_at="2026-01-01T00:00:00+00:00",
                raw_source_id="id-1",
                value=" ",
                confidence=0.5,
                review_at=None,
                expires_at=None,
            )
        )


@pytest.mark.unit
def test_normalize_candidate_handles_invalid_domain_gracefully() -> None:
    candidate = normalize_candidate(
        source_id="demo",
        name="Acme",
        domain="not a domain",
        website="https://acme.example.com",
    )
    assert candidate.domain == "acme.example.com"


@pytest.mark.unit
def test_api_adapter_reports_json_parse_failures() -> None:
    adapter = build_api_adapter(
        source_id="broken_api",
        api_url="https://api.example.com/broken.json",
        documented=True,
    )
    fetcher = HttpFetcher(fixture_loader=lambda _url: b"not-json")
    result = run_adapter(adapter, fetcher=fetcher)
    assert result.errors[0].code == "parse_failed"


@pytest.mark.unit
def test_api_adapter_accepts_top_level_list_payload() -> None:
    from app.discovery.adapters.api import parse_api_companies

    rows = parse_api_companies(
        b'[{"id": "1", "name": "Listed Co", "domain": "listed.example.com"}]'
    )
    assert rows[0]["name"] == "Listed Co"


@pytest.mark.unit
def test_rss_adapter_tolerates_invalid_checkpoint_cursor() -> None:
    adapter = build_rss_adapter(
        source_id="bad_cursor_feed",
        feed_url="https://feeds.example.com/signals.xml",
        documented=True,
    )
    checkpoint = DiscoveryCheckpoint(cursor="not-a-number")
    result = run_adapter(adapter, checkpoint=checkpoint, fetcher=_fixture_fetcher())
    assert len(result.candidates) == 2


@pytest.mark.unit
def test_sitemap_adapter_reports_normalize_failures() -> None:
    adapter = build_sitemap_adapter(
        source_id="bad_urls",
        sitemap_url="https://directory.example.com/bad-sitemap.xml",
        documented=True,
    )
    fetcher = HttpFetcher(
        fixture_loader=lambda _url: b"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>not-a-valid-url</loc></url>
        </urlset>""",
    )
    result = run_adapter(adapter, fetcher=fetcher)
    assert result.partial_failure is True
    assert any(error.code == "normalize_failed" for error in result.errors)


@pytest.mark.unit
def test_validate_public_url_rejects_credentials() -> None:
    with pytest.raises(FetchError, match="credentials"):
        validate_public_url("https://user:pass@example.com/data")


@pytest.mark.unit
def test_rate_limiter_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(requests_per_minute=0)
