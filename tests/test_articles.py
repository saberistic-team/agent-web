"""Tests for the /insights authority-content system."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import articles
from app.main import app, insight_article, insights_feed, insights_index, sitemap

client = TestClient(app)
BASE_URL = "http://testserver"


@pytest.mark.unit
def test_list_articles_newest_first() -> None:
    slugs = [article.slug for article in articles.list_articles()]
    assert slugs == [
        "fintech-architecture-due-diligence",
        "competing-sources-of-truth",
    ]


@pytest.mark.unit
def test_get_article_known_and_unknown() -> None:
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    assert article.title.startswith("Five signs")
    assert articles.get_article("missing-slug") is None


@pytest.mark.unit
def test_render_insights_index_includes_articles_and_metadata() -> None:
    html = articles.render_insights_index(BASE_URL)
    assert "Insights" in html
    assert 'href="/insights/competing-sources-of-truth"' in html
    assert 'href="/insights/fintech-architecture-due-diligence"' in html
    assert 'rel="canonical" href="http://testserver/insights"' in html
    assert 'class="top-link" href="/insights"' in html


@pytest.mark.unit
def test_render_article_page_has_social_and_semantic_markup() -> None:
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    html = articles.render_article_page(article, BASE_URL)
    assert article.title in html
    assert article.audience in html
    assert article.problem in html
    assert 'property="og:type" content="article"' in html
    assert 'property="og:url"' in html
    assert 'name="twitter:card" content="summary"' in html
    assert 'type="application/ld+json"' in html
    assert 'itemtype="https://schema.org/Article"' in html
    assert article.cta_text in html
    assert f'href="{article.cta_href}"' in html
    assert "Customer balances change depending on which screen you open" in html


@pytest.mark.unit
def test_render_sitemap_includes_static_and_article_urls() -> None:
    xml = articles.render_sitemap(BASE_URL)
    assert "<urlset" in xml
    assert "http://testserver/</loc>" in xml
    assert "http://testserver/about</loc>" in xml
    assert "http://testserver/insights</loc>" in xml
    assert "http://testserver/insights/competing-sources-of-truth</loc>" in xml


@pytest.mark.unit
def test_render_atom_feed_lists_entries() -> None:
    xml = articles.render_atom_feed(BASE_URL)
    assert "<feed" in xml
    assert "competing-sources-of-truth" in xml
    assert "fintech-architecture-due-diligence" in xml
    assert "Architecture judgment" not in xml


@pytest.mark.unit
def test_insights_index_handler() -> None:
    response = insights_index()
    assert "Insights" in response.body.decode()


@pytest.mark.unit
def test_insight_article_handler_404() -> None:
    with pytest.raises(Exception) as exc_info:
        insight_article("not-real")
    assert "404" in str(exc_info.value)


@pytest.mark.unit
def test_sitemap_handler_media_type() -> None:
    response = sitemap()
    assert response.media_type == "application/xml"
    assert b"<urlset" in response.body


@pytest.mark.unit
def test_insights_feed_handler_media_type() -> None:
    response = insights_feed()
    assert response.media_type == "application/atom+xml"
    assert b"<feed" in response.body


@pytest.mark.unit
def test_insights_index_route() -> None:
    response = client.get("/insights")
    assert response.status_code == 200
    body = response.text
    assert "Five signs an MVP has competing sources of truth" in body
    assert "What investors should examine before funding fintech architecture" in body


@pytest.mark.unit
def test_insight_article_route() -> None:
    response = client.get("/insights/competing-sources-of-truth")
    assert response.status_code == 200
    body = response.text
    assert "Five signs an MVP has competing sources of truth" in body
    assert "Request an architecture review" in body
    assert 'rel="canonical"' in body


@pytest.mark.unit
def test_insight_article_route_not_found() -> None:
    response = client.get("/insights/does-not-exist")
    assert response.status_code == 404


@pytest.mark.unit
def test_sitemap_route() -> None:
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "/insights/fintech-architecture-due-diligence" in response.text


@pytest.mark.unit
def test_atom_feed_route() -> None:
    response = client.get("/insights/feed.atom")
    assert response.status_code == 200
    assert "atom+xml" in response.headers["content-type"]
    assert "competing-sources-of-truth" in response.text


@pytest.mark.unit
def test_home_links_to_insights() -> None:
    body = client.get("/").text
    assert 'href="/insights"' in body
    assert "Insights" in body


@pytest.mark.unit
def test_launch_articles_published() -> None:
    """Issue #69 requires at least two reviewed launch articles."""
    slugs = {article.slug for article in articles.ARTICLES}
    assert "competing-sources-of-truth" in slugs
    assert "fintech-architecture-due-diligence" in slugs
    for slug in ("competing-sources-of-truth", "fintech-architecture-due-diligence"):
        article = articles.get_article(slug)
        assert article is not None
        assert article.audience
        assert article.problem
        assert article.cta_text
        assert article.cta_href
        assert article.body_html()
