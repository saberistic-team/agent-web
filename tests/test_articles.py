"""Unit tests for article data and rendering."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import articles


@pytest.mark.unit
def test_load_articles_has_required_fields() -> None:
    loaded = articles.load_articles()
    assert len(loaded) >= 2
    slugs = {article["slug"] for article in loaded}
    assert {
        "mvp-competing-sources-of-truth",
        "fractional-principal-first-30-days",
    }.issubset(slugs)
    for article in loaded:
        assert article["audience"] in articles.AUDIENCE_LABELS
        for key in articles.REQUIRED_FIELDS:
            assert article[key]
        assert len(article["sections"]) >= 1


@pytest.mark.unit
def test_get_article_found_and_missing(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
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
        ]
    }
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    found = articles.get_article("sample", path=path)
    assert found is not None
    assert found["title"] == "Sample title"
    assert articles.get_article("missing", path=path) is None


@pytest.mark.unit
def test_load_articles_rejects_duplicate_slug(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "dup",
                "title": "A",
                "meta_description": "M",
                "audience": "founders",
                "published_date": "2026-07-01",
                "author": "Author",
                "problem": "P",
                "sections": [{"heading": "H", "body": "B"}],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
            {
                "slug": "dup",
                "title": "B",
                "meta_description": "M2",
                "audience": "investors",
                "published_date": "2026-07-02",
                "author": "Author",
                "problem": "P2",
                "sections": [{"heading": "H2", "body": "B2"}],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
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


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "xss",
                "title": "Title<script>",
                "meta_description": "Meta<script>",
                "audience": "founders",
                "published_date": "2026-07-01",
                "author": "Author<script>",
                "problem": "Prob<script>",
                "sections": [{"heading": "Head<script>", "body": "Body<script>"}],
                "cta_label": "CTA<script>",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
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
    assert 'type="application/atom+xml"' in html
    assert "/insights/mvp-competing-sources-of-truth" in html
    assert "/insights/fractional-principal-first-30-days" in html


@pytest.mark.unit
def test_render_atom_feed() -> None:
    xml = articles.render_atom_feed()
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    assert len(entries) >= 2
    titles = [entry.find("atom:title", ns).text for entry in entries]
    assert "Five signs an MVP has competing sources of truth" in titles


@pytest.mark.unit
def test_list_featured_slugs() -> None:
    featured = articles.list_featured_slugs()
    assert featured == [
        "mvp-competing-sources-of-truth",
        "fractional-principal-first-30-days",
    ]
