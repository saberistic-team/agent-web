"""Article data and HTML rendering for /insights pages."""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from app.metadata import article_json_ld, social_meta_tags, web_page_json_ld
from app.seo import CANONICAL_BASE, canonical_url

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "articles.json"
INSIGHTS_INDEX_PATH = "/insights"
AUTHOR_DEFAULT = "AmirSaber Sharifi"


def load_articles(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate articles from JSON."""
    source = path or DATA_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    articles = raw.get("articles")
    if not isinstance(articles, list) or not articles:
        raise ValueError("articles.json must contain a non-empty articles array")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in articles:
        if not isinstance(item, dict):
            raise ValueError("each article must be an object")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("each article requires a slug")
        if slug in seen:
            raise ValueError(f"duplicate article slug: {slug}")
        seen.add(slug)

        for key in (
            "title",
            "meta_description",
            "audience",
            "problem",
            "published_at",
            "author",
            "eyebrow",
            "cta_label",
            "cta_href",
        ):
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"article {slug} missing or empty field: {key}")

        sections = item.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"article {slug} requires a non-empty sections array")
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"article {slug} sections must be objects")
            for section_key in ("heading", "content"):
                if not isinstance(section.get(section_key), str) or not section[section_key].strip():
                    raise ValueError(
                        f"article {slug} section missing or empty field: {section_key}"
                    )

        validated.append(item)

    validated.sort(key=lambda article: article["published_at"], reverse=True)
    return validated


def get_article(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a single article by slug, or None if not found."""
    for article in load_articles(path):
        if article["slug"] == slug:
            return article
    return None


def article_path(slug: str) -> str:
    return f"{INSIGHTS_INDEX_PATH}/{slug}"


def _head_block(
    *,
    title: str,
    description: str,
    canonical: str,
    og_type: str,
    json_ld: str,
    feed_link: bool = False,
) -> str:
    feed = ""
    if feed_link:
        feed = (
            '    <link rel="alternate" type="application/atom+xml" '
            f'title="saberistic insights" href="{CANONICAL_BASE}/insights.atom" />\n'
        )
    return f"""  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)}</title>
    <meta name="description" content="{html.escape(description)}" />
    <link rel="canonical" href="{html.escape(canonical, quote=True)}" />
{social_meta_tags(title=title, description=description, url=canonical, og_type=og_type)}
{json_ld}
{feed}    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>"""


def _site_header(*, nav_href: str, nav_label: str) -> str:
    return f"""    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
      <a class="top-link" href="{html.escape(nav_href, quote=True)}">{html.escape(nav_label)}</a>
    </header>"""


def _format_published(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return parsed.strftime("%B %Y")


def render_article_page(article: dict[str, Any]) -> str:
    """Render a full HTML page for one article."""
    slug = article["slug"]
    title = article["title"]
    meta = article["meta_description"]
    audience = article["audience"]
    problem = article["problem"]
    author = article["author"]
    eyebrow = article["eyebrow"]
    published = _format_published(article["published_at"])
    cta_label = article["cta_label"]
    cta_href = article["cta_href"]
    page_title = f"{title} · saberistic"
    canonical = canonical_url(article_path(slug))

    sections_html = "\n".join(
        f"""          <section class="article-section" aria-labelledby="section-{idx}-title">
            <h2 class="article-section-title" id="section-{idx}-title">{html.escape(section['heading'])}</h2>
            <p>{html.escape(section['content'])}</p>
          </section>"""
        for idx, section in enumerate(article["sections"])
    )

    json_ld = article_json_ld(
        title=title,
        description=meta,
        url=canonical,
        author=author,
        date_published=article["published_at"],
        date_modified=article.get("updated_at"),
    )

    return f"""<!DOCTYPE html>
<html lang="en">
{_head_block(title=page_title, description=meta, canonical=canonical, og_type="article", json_ld=json_ld, feed_link=True)}
  <body>
{_site_header(nav_href=INSIGHTS_INDEX_PATH, nav_label="Insights")}
    <main>
      <article class="block article" itemscope itemtype="https://schema.org/Article" data-slug="{html.escape(slug)}">
        <p class="article-eyebrow">{html.escape(eyebrow)}</p>
        <h1 class="page-title article-title" itemprop="headline">{html.escape(title)}</h1>
        <p class="article-meta">
          <span itemprop="author" itemscope itemtype="https://schema.org/Person">
            <span itemprop="name">{html.escape(author)}</span>
          </span>
          · <time datetime="{html.escape(article['published_at'])}" itemprop="datePublished">{html.escape(published)}</time>
        </p>
        <div class="article-audience">
          <p><strong>Audience:</strong> {html.escape(audience)}</p>
          <p><strong>Problem:</strong> {html.escape(problem)}</p>
        </div>
        <div class="article-prose" itemprop="articleBody">
{sections_html}
        </div>
        <p class="article-cta-row">
          <a class="cta" href="{html.escape(cta_href, quote=True)}">{html.escape(cta_label)}</a>
          <a class="cta cta-secondary" href="{INSIGHTS_INDEX_PATH}">All insights</a>
        </p>
      </article>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
      <p><a href="{INSIGHTS_INDEX_PATH}">Insights</a></p>
    </footer>
  </body>
</html>
"""


def render_index_page(articles: list[dict[str, Any]] | None = None) -> str:
    """Render the insights index listing all articles."""
    items = articles if articles is not None else load_articles()
    title = "Insights — saberistic"
    description = (
        "Architecture judgment for founders, investors, and technical leaders — "
        "patterns, risks, and decisions from high-stakes fintech and digital-asset work."
    )
    canonical = canonical_url(INSIGHTS_INDEX_PATH)

    list_html = "\n".join(
        f"""          <li class="article-list-item">
            <a class="article-list-link" href="{article_path(article['slug'])}">
              <span class="article-list-title">{html.escape(article['title'])}</span>
              <span class="article-list-meta">{html.escape(article['eyebrow'])} · {_format_published(article['published_at'])}</span>
            </a>
          </li>"""
        for article in items
    )

    json_ld = web_page_json_ld(title=title, description=description, url=canonical)

    return f"""<!DOCTYPE html>
<html lang="en">
{_head_block(title=title, description=description, canonical=canonical, og_type="website", json_ld=json_ld, feed_link=True)}
  <body>
{_site_header(nav_href="/", nav_label="Home")}
    <main>
      <section class="block" aria-labelledby="insights-title">
        <h1 class="page-title" id="insights-title">Insights</h1>
        <p class="article-index-lede">
          Practical architecture judgment for qualified inbound leads — no client
          names, no confidential employer details.
        </p>
        <ul class="article-list">
{list_html}
        </ul>
        <p class="article-cta-row">
          <a class="cta" href="/brief">Request architecture diagnostic</a>
        </p>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
      <p><a href="{INSIGHTS_INDEX_PATH}">Insights</a></p>
    </footer>
  </body>
</html>
"""


def atom_feed(articles: list[dict[str, Any]] | None = None) -> str:
    """Render an Atom feed for published insights."""
    items = articles if articles is not None else load_articles()
    updated = max(article["published_at"] for article in items)
    entries = []
    for article in items:
        link = canonical_url(article_path(article["slug"]))
        entries.append(
            "  <entry>\n"
            f"    <title>{xml_escape(article['title'])}</title>\n"
            f"    <link href=\"{xml_escape(link)}\" />\n"
            f"    <id>{xml_escape(link)}</id>\n"
            f"    <updated>{xml_escape(article['published_at'])}T00:00:00Z</updated>\n"
            f"    <summary>{xml_escape(article['meta_description'])}</summary>\n"
            f"    <author><name>{xml_escape(article['author'])}</name></author>\n"
            "  </entry>"
        )
    feed_url = canonical_url("/insights.atom")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>saberistic insights</title>\n"
        f"  <link href=\"{xml_escape(feed_url)}\" rel=\"self\" />\n"
        f"  <link href=\"{xml_escape(canonical_url(INSIGHTS_INDEX_PATH))}\" />\n"
        f"  <updated>{xml_escape(updated)}T00:00:00Z</updated>\n"
        "  <id>https://saberistic.com/insights</id>\n"
        f"{chr(10).join(entries)}\n"
        "</feed>\n"
    )
