"""Unit tests for article data and rendering."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import articles
from app.main import app

client = TestClient(app)

SAMPLE_ARTICLE = {
    "slug": "sample-insight",
    "title": "Sample insight title",
    "meta_description": "Sample meta description for tests.",
    "audience": "Founders",
    "problem": "A recognizable architecture problem.",
    "published_at": "2026-07-01",
    "author": "AmirSaber Sharifi",
    "eyebrow": "Architecture",
    "sections": [
        {"heading": "First sign", "content": "First paragraph."},
        {"heading": "Second sign", "content": "Second paragraph."},
    ],
    "cta_label": "Request architecture diagnostic",
    "cta_href": "/brief",
}


@pytest.mark.unit
def test_load_articles_has_required_fields() -> None:
    loaded = articles.load_articles()
    assert len(loaded) >= 2
    slugs = {article["slug"] for article in loaded}
    assert {
        "mvp-competing-sources-of-truth",
        "fintech-architecture-investor-diligence",
    }.issubset(slugs)
    for article in loaded:
        assert article["sections"]
        assert article["cta_href"].startswith("/")


@pytest.mark.unit
def test_get_article_found_and_missing(tmp_path: Path) -> None:
    data = {"articles": [SAMPLE_ARTICLE]}
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    found = articles.get_article("sample-insight", path=path)
    assert found is not None
    assert found["title"] == "Sample insight title"
    assert articles.get_article("missing", path=path) is None


@pytest.mark.unit
def test_load_articles_rejects_duplicate_slug(tmp_path: Path) -> None:
    duplicate = {**SAMPLE_ARTICLE, "title": "Duplicate"}
    data = {"articles": [SAMPLE_ARTICLE, duplicate]}
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        articles.load_articles(path=path)


@pytest.mark.unit
def test_render_article_page_structure() -> None:
    article = articles.get_article("mvp-competing-sources-of-truth")
    assert article is not None
    html = articles.render_article_page(article)
    assert "Five signs an MVP has competing sources of truth" in html
    assert 'property="og:type" content="article"' in html
    assert 'rel="canonical" href="https://saberistic.com/insights/mvp-competing-sources-of-truth"' in html
    assert 'itemtype="https://schema.org/Article"' in html
    assert "Audience:" in html
    assert "Problem:" in html
    assert 'href="/brief"' in html
    assert 'id="section-0-title"' in html
    assert "All insights" in html


@pytest.mark.unit
def test_render_index_page_lists_articles() -> None:
    html = articles.render_index_page()
    assert "<h1" in html and "Insights" in html
    assert "/insights/mvp-competing-sources-of-truth" in html
    assert "/insights/fintech-architecture-investor-diligence" in html
    assert 'type="application/atom+xml"' in html


@pytest.mark.unit
def test_load_articles_rejects_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "articles.json"
    path.write_text(json.dumps({"articles": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        articles.load_articles(path=path)

    path.write_text(json.dumps({"articles": ["bad"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        articles.load_articles(path=path)

    path.write_text(json.dumps({"articles": [{"slug": ""}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="slug"):
        articles.load_articles(path=path)

    incomplete = {**SAMPLE_ARTICLE, "sections": []}
    path.write_text(json.dumps({"articles": [incomplete]}), encoding="utf-8")
    with pytest.raises(ValueError, match="sections"):
        articles.load_articles(path=path)

    bad_section = {**SAMPLE_ARTICLE, "sections": [{"heading": "Only heading"}]}
    path.write_text(json.dumps({"articles": [bad_section]}), encoding="utf-8")
    with pytest.raises(ValueError, match="content"):
        articles.load_articles(path=path)


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    xss_article = {
        **SAMPLE_ARTICLE,
        "slug": "xss",
        "title": "Title<script>",
        "meta_description": "Meta<script>",
        "audience": "Audience<script>",
        "problem": "Problem<script>",
        "sections": [{"heading": "H<script>", "content": "Body<script>"}],
        "cta_label": "CTA<script>",
    }
    path = tmp_path / "articles.json"
    path.write_text(json.dumps({"articles": [xss_article]}), encoding="utf-8")
    article = articles.get_article("xss", path=path)
    assert article is not None
    rendered = articles.render_article_page(article)
    assert "Title&lt;script&gt;" in rendered
    assert "Audience&lt;script&gt;" in rendered
    assert "Body&lt;script&gt;" in rendered
    assert "CTA&lt;script&gt;" in rendered


@pytest.mark.unit
def test_atom_feed_contains_entries() -> None:
    feed = articles.atom_feed()
    root = ET.fromstring(feed)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    titles = [entry.find("atom:title", ns).text for entry in root.findall("atom:entry", ns)]
    assert "Five signs an MVP has competing sources of truth" in titles
    assert "What investors should examine before funding fintech architecture" in titles


@pytest.mark.unit
def test_insights_index_route() -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    body = response.text
    assert "Insights" in body
    assert "/insights/mvp-competing-sources-of-truth" in body
    assert 'rel="canonical" href="https://saberistic.com/insights"' in body


@pytest.mark.unit
def test_insight_article_route() -> None:
    response = client.get("/insights/fintech-architecture-investor-diligence")
    assert response.status_code == 200
    body = response.text
    assert "What investors should examine before funding fintech architecture" in body
    assert "Ledger and balance boundaries" in body
    assert 'property="og:type" content="article"' in body
    assert "Discuss technical due diligence" in body


@pytest.mark.unit
def test_insight_article_not_found() -> None:
    response = client.get("/insights/does-not-exist")
    assert response.status_code == 404


@pytest.mark.unit
def test_insights_atom_route() -> None:
    response = client.get("/insights.atom")
    assert response.status_code == 200
    assert "application/atom+xml" in response.headers["content-type"]
    assert "Five signs an MVP has competing sources of truth" in response.text
