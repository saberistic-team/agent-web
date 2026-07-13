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
def test_load_articles_has_published_launch_content() -> None:
    articles = insights.load_articles()
    published = insights.load_published_articles()
    assert len(published) >= 2
    slugs = {article["slug"] for article in published}
    assert "competing-sources-of-truth" in slugs
    assert "fintech-architecture-diligence" in slugs
    for article in articles:
        assert article["status"] in ("published", "draft")
        assert article["sections"]


@pytest.mark.unit
def test_get_article_published_only() -> None:
    found = insights.get_article("competing-sources-of-truth")
    assert found is not None
    assert found["status"] == "published"
    assert insights.get_article("empty-wallet-active-positions") is None
    assert insights.get_article("missing-slug") is None


@pytest.mark.unit
def test_load_articles_rejects_duplicate_slug(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "dup",
                "status": "published",
                "title": "One",
                "eyebrow": "Architecture",
                "published": "2026-07-01",
                "audience": "Founders",
                "problem": "Problem",
                "meta_description": "Meta",
                "summary": "Summary",
                "sections": [{"heading": "H", "body": "B"}],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
            {
                "slug": "dup",
                "status": "draft",
                "title": "Two",
                "eyebrow": "Architecture",
                "published": "2026-07-02",
                "audience": "Founders",
                "problem": "Problem",
                "meta_description": "Meta",
                "summary": "Summary",
                "sections": [{"heading": "H", "body": "B"}],
                "cta_label": "Go",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        insights.load_articles(path=path)


@pytest.mark.unit
def test_render_insight_page_structure() -> None:
    article = insights.get_article("competing-sources-of-truth")
    assert article is not None
    html = insights.render_insight_page(article)
    assert "Five signs an MVP has competing sources of truth" in html
    assert 'rel="canonical" href="https://saberistic.com/insights/competing-sources-of-truth"' in html
    assert 'property="og:type" content="article"' in html
    assert '"@type": "Article"' in html
    assert "<strong>Audience:</strong>" in html
    assert "<strong>Problem:</strong>" in html
    assert 'href="/brief"' in html
    assert 'href="/insights"' in html


@pytest.mark.unit
def test_render_insights_index_lists_published() -> None:
    html = insights.render_insights_index()
    assert "Insights — saberistic" in html
    assert "/insights/competing-sources-of-truth" in html
    assert "/insights/fintech-architecture-diligence" in html
    assert "empty-wallet-active-positions" not in html
    assert 'rel="canonical" href="https://saberistic.com/insights"' in html


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "articles": [
            {
                "slug": "xss",
                "status": "published",
                "title": "Title<script>",
                "eyebrow": "Architecture",
                "published": "2026-07-01",
                "audience": "Audience<script>",
                "problem": "Problem<script>",
                "meta_description": "Meta<script>",
                "summary": "Summary<script>",
                "sections": [{"heading": "Head<script>", "body": "Body<script>"}],
                "cta_label": "CTA<script>",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    article = insights.get_article("xss", path=path)
    assert article is not None
    rendered = insights.render_insight_page(article)
    assert "Title&lt;script&gt;" in rendered
    assert "Audience&lt;script&gt;" in rendered
    assert "Body&lt;script&gt;" in rendered
    assert "Title<script>" not in rendered.split("</head>", 1)[-1]


@pytest.mark.unit
def test_atom_feed_xml() -> None:
    xml = insights.atom_feed_xml()
    assert '<?xml version="1.0"' in xml
    assert 'xmlns="http://www.w3.org/2005/Atom"' in xml
    assert "competing-sources-of-truth" in xml
    assert "fintech-architecture-diligence" in xml
    assert "empty-wallet-active-positions" not in xml


@pytest.mark.unit
def test_insights_index_route() -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    body = response.text
    assert "Insights" in body
    assert "/insights/competing-sources-of-truth" in body


@pytest.mark.unit
def test_insight_article_route() -> None:
    response = client.get("/insights/competing-sources-of-truth")
    assert response.status_code == 200
    body = response.text
    assert "competing sources of truth" in body
    assert 'property="og:type" content="article"' in body
    assert "Start Architecture Diagnostic" in body


@pytest.mark.unit
def test_insight_draft_returns_404() -> None:
    response = client.get("/insights/empty-wallet-active-positions")
    assert response.status_code == 404


@pytest.mark.unit
def test_insight_not_found() -> None:
    response = client.get("/insights/does-not-exist")
    assert response.status_code == 404


@pytest.mark.unit
def test_insights_feed_route() -> None:
    response = client.get("/insights/feed.xml")
    assert response.status_code == 200
    assert "application/atom+xml" in response.headers["content-type"]
    assert "competing-sources-of-truth" in response.text


@pytest.mark.unit
def test_lastmod_for_path_uses_publish_date() -> None:
    from datetime import date

    mod = insights.lastmod_for_path("/insights/competing-sources-of-truth")
    assert mod == date(2026, 7, 10)
