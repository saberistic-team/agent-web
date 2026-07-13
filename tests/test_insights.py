"""Unit tests for insight article data and rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import insights
from app.main import app

client = TestClient(app)


@pytest.mark.unit
def test_load_insights_has_launch_articles() -> None:
    articles = insights.load_insights()
    assert len(articles) >= 2
    slugs = {article["slug"] for article in articles}
    assert {"empty-wallets-active-positions", "mvp-competing-sources-of-truth"}.issubset(slugs)
    for article in articles:
        for key in insights.REQUIRED_FIELDS:
            assert article[key]


@pytest.mark.unit
def test_list_published_insights_excludes_drafts(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "published-one",
                "status": "published",
                "title": "Published",
                "published_at": "2026-07-13",
                "audience": "Founders",
                "problem": "Problem",
                "meta_description": "Meta",
                "excerpt": "Excerpt",
                "paragraphs": ["Body"],
                "cta_label": "CTA",
                "cta_href": "/brief",
            },
            {
                "slug": "draft-one",
                "status": "draft",
                "title": "Draft",
                "published_at": "2026-07-12",
                "audience": "Investors",
                "problem": "Problem",
                "meta_description": "Meta",
                "excerpt": "Excerpt",
                "paragraphs": ["Body"],
                "cta_label": "CTA",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    published = insights.list_published_insights(path=path)
    assert [a["slug"] for a in published] == ["published-one"]
    assert insights.get_insight("draft-one", path=path) is None
    assert insights.get_insight("published-one", path=path) is not None


@pytest.mark.unit
def test_load_insights_rejects_duplicate_slug(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "dup",
                "title": "A",
                "published_at": "2026-07-13",
                "audience": "Founders",
                "problem": "P",
                "meta_description": "M",
                "excerpt": "E",
                "paragraphs": ["Body"],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
            {
                "slug": "dup",
                "title": "B",
                "published_at": "2026-07-12",
                "audience": "Founders",
                "problem": "P2",
                "meta_description": "M2",
                "excerpt": "E2",
                "paragraphs": ["Body2"],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        insights.load_insights(path=path)


@pytest.mark.unit
def test_render_insight_page_structure() -> None:
    article = insights.get_insight("empty-wallets-active-positions")
    assert article is not None
    html = insights.render_insight_page(article)
    assert "Why empty wallets sometimes show active positions" in html
    assert 'property="og:type" content="article"' in html
    assert 'rel="canonical" href="https://saberistic.com/insights/empty-wallets-active-positions"' in html
    assert '"@type": "Article"' in html
    assert 'href="/brief"' in html
    assert "Competing read paths" in html
    assert 'class="insight-problem"' in html


@pytest.mark.unit
def test_render_insights_index_lists_articles() -> None:
    html = insights.render_insights_index()
    assert "Insights — saberistic" in html
    assert "/insights/empty-wallets-active-positions" in html
    assert "/insights/mvp-competing-sources-of-truth" in html
    assert 'rel="alternate" type="application/atom+xml"' in html


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "xss",
                "status": "published",
                "title": "Title<script>",
                "published_at": "2026-07-13",
                "audience": "Audience<script>",
                "problem": "Problem<script>",
                "meta_description": "Meta<script>",
                "excerpt": "Excerpt<script>",
                "paragraphs": ["Para<script>"],
                "sections": [{"title": "Sec<script>", "paragraphs": ["Body<script>"]}],
                "cta_label": "CTA<script>",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    article = insights.get_insight("xss", path=path)
    assert article is not None
    rendered = insights.render_insight_page(article)
    assert "<script>" not in rendered.replace('type="application/ld+json"', "")
    assert "&lt;script&gt;" in rendered


@pytest.mark.unit
def test_render_atom_feed() -> None:
    feed = insights.render_atom_feed()
    assert feed.startswith('<?xml version="1.0"')
    assert "<feed xmlns=" in feed
    assert "empty-wallets-active-positions" in feed
    assert "mvp-competing-sources-of-truth" in feed


@pytest.mark.unit
def test_insights_index_route() -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    body = response.text
    assert "Insights" in body
    assert "/insights/empty-wallets-active-positions" in body
    assert 'rel="canonical" href="https://saberistic.com/insights"' in body


@pytest.mark.unit
def test_insight_article_route() -> None:
    response = client.get("/insights/mvp-competing-sources-of-truth")
    assert response.status_code == 200
    body = response.text
    assert "Five signs an MVP has competing sources of truth" in body
    assert "Start Architecture Diagnostic" in body
    assert 'property="og:type" content="article"' in body


@pytest.mark.unit
def test_insight_not_found() -> None:
    response = client.get("/insights/does-not-exist")
    assert response.status_code == 404


@pytest.mark.unit
def test_insights_feed_route() -> None:
    response = client.get("/insights/feed.xml")
    assert response.status_code == 200
    assert "application/atom+xml" in response.headers["content-type"]
    assert "<entry>" in response.text


@pytest.mark.unit
def test_each_article_has_single_primary_cta() -> None:
    for article in insights.list_published_insights():
        html = insights.render_insight_page(article)
        assert html.count('class="cta" href=') == 1
