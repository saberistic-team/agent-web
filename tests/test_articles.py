"""Unit tests for article data and rendering."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import articles
from app.main import app, insight_article, insights_feed, insights_index

client = TestClient(app)

SAMPLE_ARTICLE = {
    "slug": "sample",
    "title": "Sample title",
    "meta_description": "Meta",
    "audience": "founders",
    "published_date": "2026-07-01",
    "author": "AmirSaber Sharifi",
    "problem": "Problem statement",
    "sections": [{"heading": "One", "body": "Body text"}],
    "cta_label": "CTA",
    "cta_href": "/brief",
}


def _write_articles(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.unit
def test_load_articles_has_required_fields() -> None:
    loaded = articles.load_articles()
    assert len(loaded) >= 2
    slugs = {article["slug"] for article in loaded}
    assert {
        "mvp-competing-sources-of-truth",
        "fintech-architecture-due-diligence",
    }.issubset(slugs)
    for article in loaded:
        assert article["audience"] in articles.AUDIENCE_LABELS
        for key in articles.REQUIRED_FIELDS:
            assert article[key]
        assert len(article["sections"]) >= 1


@pytest.mark.unit
def test_get_article_found_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "articles.json"
    _write_articles(path, {"articles": [SAMPLE_ARTICLE]})

    found = articles.get_article("sample", path=path)
    assert found is not None
    assert found["title"] == "Sample title"
    assert articles.get_article("missing", path=path) is None


@pytest.mark.unit
def test_load_articles_rejects_duplicate_slug(tmp_path: Path) -> None:
    dup = {**SAMPLE_ARTICLE, "slug": "dup"}
    other = {**SAMPLE_ARTICLE, "slug": "dup", "title": "B", "audience": "investors"}
    path = tmp_path / "articles.json"
    _write_articles(path, {"articles": [dup, other]})
    with pytest.raises(ValueError, match="duplicate"):
        articles.load_articles(path=path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,match",
    [
        ({"articles": []}, "non-empty"),
        ({"articles": ["bad"]}, "object"),
        ({"articles": [{**SAMPLE_ARTICLE, "slug": ""}]}, "slug"),
        ({"articles": [{**SAMPLE_ARTICLE, "audience": "unknown"}]}, "invalid audience"),
        ({"articles": [{**SAMPLE_ARTICLE, "title": ""}]}, "title"),
        ({"articles": [{**SAMPLE_ARTICLE, "sections": []}]}, "sections"),
        (
            {"articles": [{**SAMPLE_ARTICLE, "sections": [{"heading": "H", "body": ""}]}]},
            "body",
        ),
        (
            {"articles": [{**SAMPLE_ARTICLE, "sections": ["not-a-dict"]}]},
            "objects",
        ),
    ],
)
def test_load_articles_validation_errors(
    tmp_path: Path, payload: dict, match: str
) -> None:
    path = tmp_path / "articles.json"
    _write_articles(path, payload)
    with pytest.raises(ValueError, match=match):
        articles.load_articles(path=path)


@pytest.mark.unit
def test_render_article_page_structure() -> None:
    article = articles.get_article("mvp-competing-sources-of-truth")
    assert article is not None
    html = articles.render_article_page(article)
    assert "<title>Five signs an MVP has competing sources of truth · saberistic</title>" in html
    assert 'property="og:type" content="article"' in html
    assert 'name="twitter:card"' in html
    assert 'rel="canonical" href="https://saberistic.com/insights/mvp-competing-sources-of-truth"' in html
    assert '"@type": "Article"' in html
    assert "datePublished" in html
    assert 'class="block article"' in html
    assert 'href="/brief"' in html
    assert 'id="section-0-title"' in html
    assert "All insights" not in html
    assert html.count('class="cta"') == 1


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                **SAMPLE_ARTICLE,
                "slug": "xss",
                "title": "Title<script>",
                "meta_description": "Meta<script>",
                "author": "Author<script>",
                "problem": "Prob<script>",
                "sections": [{"heading": "Head<script>", "body": "Body<script>"}],
                "cta_label": "CTA<script>",
            }
        ]
    }
    path = tmp_path / "articles.json"
    _write_articles(path, data)
    article = articles.get_article("xss", path=path)
    assert article is not None
    rendered = articles.render_article_page(article)
    assert "Title&lt;script&gt;" in rendered
    main_start = rendered.index("<main>")
    assert "<script>" not in rendered[main_start:]


@pytest.mark.unit
def test_render_insights_index_page() -> None:
    html = articles.render_insights_index_page()
    assert "<title>Insights — saberistic</title>" in html
    assert 'rel="canonical" href="https://saberistic.com/insights"' in html
    assert 'href="/insights/feed.xml"' in html
    assert "/insights/mvp-competing-sources-of-truth" in html
    assert "/insights/fintech-architecture-due-diligence" in html


@pytest.mark.unit
def test_render_insights_index_page_custom_list() -> None:
    html = articles.render_insights_index_page([SAMPLE_ARTICLE])
    assert "Sample title" in html
    assert "/insights/sample" in html


@pytest.mark.unit
def test_render_atom_feed() -> None:
    xml = articles.render_atom_feed()
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    assert len(entries) >= 2
    titles = [entry.find("atom:title", ns).text for entry in entries]
    assert "Five signs an MVP has competing sources of truth" in titles
    self_link = root.find("atom:link[@rel='self']", ns)
    assert self_link is not None
    assert self_link.get("href") == "https://saberistic.com/insights/feed.xml"


@pytest.mark.unit
def test_render_atom_feed_custom_list() -> None:
    xml = articles.render_atom_feed([SAMPLE_ARTICLE])
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    assert len(root.findall("atom:entry", ns)) == 1


@pytest.mark.unit
def test_list_featured_slugs() -> None:
    featured = articles.list_featured_slugs()
    assert featured == [
        "mvp-competing-sources-of-truth",
        "fintech-architecture-due-diligence",
    ]


@pytest.mark.unit
def test_insights_routes() -> None:
    index = client.get("/insights")
    assert index.status_code == 200
    assert "Insights" in index.text
    assert "/insights/mvp-competing-sources-of-truth" in index.text

    article = client.get("/insights/mvp-competing-sources-of-truth")
    assert article.status_code == 200
    assert "competing sources of truth" in article.text

    feed = client.get("/insights/feed.xml")
    assert feed.status_code == 200
    assert "application/atom+xml" in feed.headers["content-type"]
    assert "Five signs an MVP" in feed.text

    missing = client.get("/insights/does-not-exist")
    assert missing.status_code == 404


@pytest.mark.unit
def test_article_single_cta() -> None:
    response = client.get("/insights/fintech-architecture-due-diligence")
    body = response.text
    assert 'class="cta"' in body
    assert body.count('class="cta"') == 1
    assert "Request technical diligence" in body


@pytest.mark.unit
def test_insights_handlers_unit() -> None:
    index = insights_index()
    assert "Insights" in index.body.decode()
    feed = insights_feed()
    assert b"<feed" in feed.body
    page = insight_article("mvp-competing-sources-of-truth")
    assert "competing sources of truth" in page.body.decode()
