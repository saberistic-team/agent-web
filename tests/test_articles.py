"""Tests for the /insights authority-content system."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import articles
from app.main import app, insight_article, insights_feed, insights_index

from app.seo import CANONICAL_BASE

client = TestClient(app)


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
def test_article_path_and_canonical_url() -> None:
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    assert article.path == "/insights/competing-sources-of-truth"
    assert (
        article.canonical_url()
        == "https://saberistic.com/insights/competing-sources-of-truth"
    )


@pytest.mark.unit
def test_body_html_missing_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    missing = replace(article, body_file="missing-body.html")
    monkeypatch.setattr(articles, "ARTICLES_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="article body missing"):
        missing.body_html()


@pytest.mark.unit
def test_render_template_unresolved_placeholder_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "broken.html").write_text("Hello {{missing}}", encoding="utf-8")
    monkeypatch.setattr(articles, "TEMPLATES_DIR", templates)
    with pytest.raises(ValueError, match="unresolved template placeholders"):
        articles._render_template("broken.html", title="ok")


@pytest.mark.unit
def test_render_insights_index_includes_articles_and_metadata() -> None:
    html = articles.render_insights_index()
    assert "Insights" in html
    assert 'href="/insights/competing-sources-of-truth"' in html
    assert 'href="/insights/fintech-architecture-due-diligence"' in html
    assert f'rel="canonical" href="{CANONICAL_BASE}/insights"' in html
    assert 'class="top-link" href="/insights"' in html


@pytest.mark.unit
def test_render_article_page_has_social_and_semantic_markup() -> None:
    article = articles.get_article("competing-sources-of-truth")
    assert article is not None
    html = articles.render_article_page(article)
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
def test_render_fintech_article_page() -> None:
    article = articles.get_article("fintech-architecture-due-diligence")
    assert article is not None
    html = articles.render_article_page(article)
    assert "Ledger design and reconciliation discipline" in html
    assert "Discuss technical diligence" in html


@pytest.mark.unit
def test_render_atom_feed_lists_entries() -> None:
    xml = articles.render_atom_feed()
    assert "<feed" in xml
    assert "competing-sources-of-truth" in xml
    assert "fintech-architecture-due-diligence" in xml
    assert "Architecture judgment" not in xml


@pytest.mark.unit
def test_insights_index_handler() -> None:
    response = insights_index()
    assert "Insights" in response.body.decode()


@pytest.mark.unit
def test_insight_article_handler_found() -> None:
    response = insight_article("competing-sources-of-truth")
    assert "Five signs an MVP has competing sources of truth" in response.body.decode()


@pytest.mark.unit
def test_insight_article_handler_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        insight_article("not-real")
    assert exc_info.value.status_code == 404


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
def test_sitemap_route_includes_insights() -> None:
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "/insights" in response.text
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
