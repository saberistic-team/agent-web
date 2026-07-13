"""Unit tests for article data and rendering."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import articles


@pytest.mark.unit
def test_load_articles_has_required_fields() -> None:
    items = articles.load_articles()
    assert len(items) >= 2
    published = [item for item in items if item["published"]]
    assert len(published) >= 2
    slugs = {item["slug"] for item in published}
    assert {"competing-sources-of-truth", "fintech-architecture-diligence"}.issubset(slugs)
    for item in items:
        for key in articles.REQUIRED_FIELDS:
            assert item[key]
        assert isinstance(item["sections"], list) and item["sections"]


@pytest.mark.unit
def test_list_published_sorted_newest_first() -> None:
    published = articles.list_published_articles()
    assert len(published) >= 2
    dates = [item["published_date"] for item in published]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.unit
def test_get_article_hides_drafts(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "live",
                "title": "Live",
                "audience": "Founders",
                "problem": "Problem statement here.",
                "meta_description": "Meta",
                "author": "Author",
                "published_date": "2026-07-01",
                "published": True,
                "sections": [{"heading": "One", "paragraphs": ["Body text."]}],
                "cta_label": "CTA",
                "cta_href": "/brief",
            },
            {
                "slug": "draft",
                "title": "Draft",
                "audience": "Founders",
                "problem": "Draft problem.",
                "meta_description": "Meta",
                "author": "Author",
                "published_date": "2026-06-01",
                "published": False,
                "sections": [{"heading": "One", "paragraphs": ["Body."]}],
                "cta_label": "CTA",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "articles.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    assert articles.get_article("live", path=path) is not None
    assert articles.get_article("draft", path=path) is None
    assert articles.get_article("missing", path=path) is None


@pytest.mark.unit
def test_load_articles_rejects_duplicate_slug(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "dup",
                "title": "A",
                "audience": "Founders",
                "problem": "P",
                "meta_description": "M",
                "author": "Author",
                "published_date": "2026-07-01",
                "published": True,
                "sections": [{"heading": "H", "paragraphs": ["P"]}],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
            {
                "slug": "dup",
                "title": "B",
                "audience": "Founders",
                "problem": "P2",
                "meta_description": "M2",
                "author": "Author",
                "published_date": "2026-07-02",
                "published": True,
                "sections": [{"heading": "H", "paragraphs": ["P"]}],
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
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    html = articles.render_article_page(article)
    assert "Five signs an MVP has competing sources of truth" in html
    assert 'rel="canonical" href="https://saberistic.com/insights/competing-sources-of-truth"' in html
    assert 'property="og:type" content="article"' in html
    assert '"@type": "Article"' in html
    assert 'itemtype="https://schema.org/Article"' in html
    assert 'href="/brief"' in html
    assert "Founders &amp; engineering leads" in html


@pytest.mark.unit
def test_render_insights_index_lists_published() -> None:
    html = articles.render_insights_index()
    assert "Insights" in html
    assert "/insights/competing-sources-of-truth" in html
    assert "/insights/fintech-architecture-diligence" in html
    assert "/insights/empty-wallet-active-positions" not in html
    assert 'rel="canonical" href="https://saberistic.com/insights"' in html


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "xss",
                "title": "Title<script>",
                "audience": "Audience<script>",
                "problem": "Problem<script>",
                "meta_description": "Meta<script>",
                "author": "Author<script>",
                "published_date": "2026-07-01",
                "published": True,
                "sections": [
                    {
                        "heading": "Head<script>",
                        "paragraphs": ["Para<script>"],
                    }
                ],
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
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.unit
def test_atom_feed_xml() -> None:
    xml = articles.atom_feed_xml()
    root = ET.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    assert root.tag.endswith("feed")
    titles = [node.text for node in root.findall("atom:entry/atom:title", ns)]
    assert "Five signs an MVP has competing sources of truth" in titles
    assert "What investors should examine before funding fintech architecture" in titles
    self_link = root.find('atom:link[@rel="self"]', ns)
    assert self_link is not None
    assert self_link.get("href") == "https://saberistic.com/insights/feed.xml"
