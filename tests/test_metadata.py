"""Tests for Open Graph, Twitter card, canonical, and JSON-LD metadata."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SITE_BASE = "https://saberistic.com"
OG_IMAGE = f"{SITE_BASE}/assets/og-share.png"
OG_IMAGE_ALT = "saberistic — high-stakes architecture and engineering leadership"

PROFESSIONAL_SERVICE_DESCRIPTION = (
    "Architecture and engineering leadership for Seed–Series B fintech, AI, "
    "digital-asset, and technically complex products."
)
PERSON_DESCRIPTION = (
    "Software architect and engineering leader helping startups resolve "
    "high-stakes architecture, reliability, security, and scaling problems."
)

FORBIDDEN_LEGACY_STRINGS = (
    "Software development — filling gaps between markets and tech",
    "filling gaps between markets and tech",
)

# Titles/descriptions match post-#68 SEO copy; OG/Twitter must stay aligned.
INDEXABLE_PAGES: dict[str, dict[str, str]] = {
    "/": {
        "title": "saberistic — technical architecture & engineering leadership",
        "description": (
            "Technical architecture and engineering leadership for seed–Series B "
            "fintech, AI, and digital-asset companies facing high-stakes scaling, "
            "security, or product-delivery problems."
        ),
        "canonical": f"{SITE_BASE}/",
    },
    "/about": {
        "title": "AmirSaber Sharifi — About",
        "description": (
            "About AmirSaber Sharifi — software engineer, architect, "
            "and founder of saberistic."
        ),
        "canonical": f"{SITE_BASE}/about",
    },
    "/services": {
        "title": "Services — saberistic",
        "description": (
            "Architecture diagnostic ($200), fractional principal architect, and "
            "technical due diligence for Seed–Series B fintech, AI, and "
            "digital-asset companies."
        ),
        "canonical": f"{SITE_BASE}/services",
    },
    "/case-studies": {
        "title": "Case studies — saberistic",
        "description": (
            "Outcome-oriented case studies — infrastructure, security, "
            "engineering leadership, and architecture diagnostics from "
            "AmirSaber Sharifi and saberistic."
        ),
        "canonical": f"{SITE_BASE}/case-studies",
    },
    "/insights": {
        "title": "Insights — saberistic",
        "description": (
            "Architecture judgment for founders, investors, and technical leaders — "
            "fintech, digital assets, and high-stakes product delivery."
        ),
        "canonical": f"{SITE_BASE}/insights",
    },
    "/brief": {
        "title": "Architecture Diagnostic — saberistic",
        "description": (
            "Architecture Diagnostic — $200 paid intake. Review of your technical "
            "problem and follow-up by email."
        ),
        "canonical": f"{SITE_BASE}/brief",
    },
}

# Payment success is shareable but must stay noindex without a canonical (#68).
NOINDEX_PAGES: dict[str, dict[str, str]] = {
    "/brief/success": {
        "title": "Payment completed — saberistic",
        "description": (
            "Your Architecture Diagnostic payment was completed. saberistic will "
            "follow up by email."
        ),
        "og_url": f"{SITE_BASE}/brief/success",
    },
}

PUBLIC_PAGES = {**INDEXABLE_PAGES, **NOINDEX_PAGES}


class _HeadParser(HTMLParser):
    """Extract head metadata from static HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._in_head = False
        self._in_title = False
        self._in_ld_json = False
        self._ld_json_chunks: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if tag == "head":
            self._in_head = True
        if not self._in_head:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attr_map.get("property") or attr_map.get("name")
            content = attr_map.get("content")
            if key and content:
                self.meta[key] = content
        elif tag == "link":
            rel = attr_map.get("rel")
            href = attr_map.get("href")
            if rel and href:
                self.links[rel] = href
        elif tag == "script" and attr_map.get("type") == "application/ld+json":
            self._in_ld_json = True
            self._ld_json_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_ld_json:
            self._in_ld_json = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_ld_json:
            self._ld_json_chunks.append(data)

    @property
    def ld_json_raw(self) -> str:
        return "".join(self._ld_json_chunks).strip()


def _parse_head(html: str) -> _HeadParser:
    parser = _HeadParser()
    parser.feed(html)
    return parser


def _collect_types(node: Any, types: list[str]) -> None:
    if isinstance(node, dict):
        schema_type = node.get("@type")
        if isinstance(schema_type, str):
            types.append(schema_type)
        elif isinstance(schema_type, list):
            types.extend(str(item) for item in schema_type)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _collect_types(item, types)
        for value in node.values():
            _collect_types(value, types)
    elif isinstance(node, list):
        for item in node:
            _collect_types(item, types)


@pytest.mark.unit
@pytest.mark.parametrize("path,expected", PUBLIC_PAGES.items())
def test_public_page_unique_title_and_description(
    path: str, expected: dict[str, str]
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    head = _parse_head(response.text)
    assert head.title.strip() == expected["title"]
    assert head.meta["description"] == expected["description"]


@pytest.mark.unit
@pytest.mark.parametrize("path,expected", PUBLIC_PAGES.items())
def test_public_page_open_graph_metadata(path: str, expected: dict[str, str]) -> None:
    response = client.get(path)
    head = _parse_head(response.text)
    assert head.meta["og:title"] == expected["title"]
    assert head.meta["og:description"] == expected["description"]
    assert head.meta["og:url"] == expected.get(
        "canonical", expected.get("og_url", "")
    )
    assert head.meta["og:type"] == "website"
    assert head.meta["og:site_name"] == "saberistic"
    assert head.meta["og:image"] == OG_IMAGE
    assert head.meta["og:image:width"] == "1200"
    assert head.meta["og:image:height"] == "630"
    assert head.meta["og:image:alt"] == OG_IMAGE_ALT


@pytest.mark.unit
@pytest.mark.parametrize("path,expected", INDEXABLE_PAGES.items())
def test_public_page_canonical_matches_og_url(
    path: str, expected: dict[str, str]
) -> None:
    response = client.get(path)
    head = _parse_head(response.text)
    assert head.links["canonical"] == expected["canonical"]
    assert head.meta["og:url"] == head.links["canonical"]


@pytest.mark.unit
def test_brief_success_has_og_url_without_canonical() -> None:
    response = client.get("/brief/success")
    head = _parse_head(response.text)
    assert "canonical" not in head.links
    assert head.meta["robots"] == "noindex, nofollow"
    assert head.meta["og:url"] == f"{SITE_BASE}/brief/success"


@pytest.mark.unit
@pytest.mark.parametrize("path,expected", PUBLIC_PAGES.items())
def test_public_page_twitter_card_metadata(path: str, expected: dict[str, str]) -> None:
    response = client.get(path)
    head = _parse_head(response.text)
    assert head.meta["twitter:card"] == "summary_large_image"
    assert head.meta["twitter:title"] == expected["title"]
    assert head.meta["twitter:description"] == expected["description"]
    assert head.meta["twitter:image"] == OG_IMAGE
    assert head.meta["twitter:image:alt"] == OG_IMAGE_ALT


@pytest.mark.unit
@pytest.mark.parametrize("path", PUBLIC_PAGES.keys())
def test_public_page_json_ld_valid(path: str) -> None:
    response = client.get(path)
    head = _parse_head(response.text)
    assert head.ld_json_raw, f"Missing JSON-LD on {path}"
    data = json.loads(head.ld_json_raw)
    assert isinstance(data, dict)
    assert data.get("@context") == "https://schema.org"


@pytest.mark.unit
def test_home_json_ld_person_and_professional_service() -> None:
    response = client.get("/")
    head = _parse_head(response.text)
    data = json.loads(head.ld_json_raw)
    types: list[str] = []
    _collect_types(data, types)
    assert "Person" in types
    assert "ProfessionalService" in types

    graph = data.get("@graph", [])
    person = next(item for item in graph if item.get("@type") == "Person")
    org = next(item for item in graph if item.get("@type") == "ProfessionalService")
    assert person["description"] == PERSON_DESCRIPTION
    assert org["description"] == PROFESSIONAL_SERVICE_DESCRIPTION


@pytest.mark.unit
@pytest.mark.parametrize("path", PUBLIC_PAGES.keys())
def test_public_pages_have_no_legacy_positioning(path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    for forbidden in FORBIDDEN_LEGACY_STRINGS:
        assert forbidden not in response.text, (
            f"Legacy positioning found on {path}: {forbidden!r}"
        )


@pytest.mark.unit
def test_insight_pages_have_no_legacy_positioning() -> None:
    for slug in ("empty-wallets-active-positions", "mvp-competing-sources-of-truth"):
        response = client.get(f"/insights/{slug}")
        assert response.status_code == 200
        for forbidden in FORBIDDEN_LEGACY_STRINGS:
            assert forbidden not in response.text, (
                f"Legacy positioning found on /insights/{slug}: {forbidden!r}"
            )
        head = _parse_head(response.text)
        assert head.meta["og:image:alt"] == OG_IMAGE_ALT
        assert head.meta["twitter:image:alt"] == OG_IMAGE_ALT


@pytest.mark.unit
def test_about_json_ld_person() -> None:
    response = client.get("/about")
    head = _parse_head(response.text)
    data = json.loads(head.ld_json_raw)
    assert data["@type"] == "Person"
    assert data["name"] == "AmirSaber Sharifi"
    assert "linkedin.com/in/saberistic" in data["sameAs"][0]


@pytest.mark.unit
def test_json_ld_has_no_invented_claims() -> None:
    """Structured data must not include ratings, awards, or fabricated credentials."""
    forbidden = re.compile(
        r"aggregateRating|award|reviewCount|priceRange|ratingValue",
        re.IGNORECASE,
    )
    for path in PUBLIC_PAGES:
        response = client.get(path)
        head = _parse_head(response.text)
        assert not forbidden.search(head.ld_json_raw), f"Forbidden claim on {path}"


@pytest.mark.unit
def test_og_share_image_asset() -> None:
    response = client.get("/assets/og-share.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content[12:16] == b"IHDR"
    width = int.from_bytes(response.content[16:20], "big")
    height = int.from_bytes(response.content[20:24], "big")
    assert (width, height) == (1200, 630)


@pytest.mark.unit
def test_public_page_titles_are_unique() -> None:
    titles = [PUBLIC_PAGES[path]["title"] for path in PUBLIC_PAGES]
    assert len(titles) == len(set(titles))
