"""Insight article data and HTML rendering for /insights pages."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from app.metadata import OG_IMAGE, OG_IMAGE_ALT
from app.seo import CANONICAL_BASE
from app.site_layout import render_site_header

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "insights.json"
DEFAULT_AUTHOR = "AmirSaber Sharifi"

REQUIRED_FIELDS = (
    "slug",
    "title",
    "published_at",
    "audience",
    "problem",
    "meta_description",
    "excerpt",
    "cta_label",
    "cta_href",
)


def _validate_paragraphs(value: Any, field: str, slug: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"article {slug} requires non-empty {field} array")
    paragraphs: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"article {slug} {field} entries must be non-empty strings")
        paragraphs.append(item)
    return paragraphs


def load_insights(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate insight articles from JSON."""
    source = path or DATA_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    articles = raw.get("articles")
    if not isinstance(articles, list) or not articles:
        raise ValueError("insights.json must contain a non-empty articles array")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in articles:
        if not isinstance(item, dict):
            raise ValueError("each article must be an object")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("each article requires a slug")
        if slug in seen:
            raise ValueError(f"duplicate insight slug: {slug}")
        seen.add(slug)

        status = item.get("status", "published")
        if status not in ("published", "draft"):
            raise ValueError(f"invalid status for {slug}: {status!r}")

        for key in REQUIRED_FIELDS:
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"article {slug} missing or empty field: {key}")

        item["paragraphs"] = _validate_paragraphs(item.get("paragraphs"), "paragraphs", slug)

        sections = item.get("sections", [])
        if not isinstance(sections, list):
            raise ValueError(f"article {slug} sections must be an array")
        validated_sections: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"article {slug} section must be an object")
            title = section.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"article {slug} section requires a title")
            paragraphs = _validate_paragraphs(
                section.get("paragraphs"), "section paragraphs", slug
            )
            validated_sections.append({"title": title, "paragraphs": paragraphs})
        item["sections"] = validated_sections

        validated.append(item)
    return validated


def list_published_insights(path: Path | None = None) -> list[dict[str, Any]]:
    """Return published articles, newest first."""
    published = [a for a in load_insights(path) if a.get("status", "published") == "published"]
    return sorted(published, key=lambda a: a["published_at"], reverse=True)


def get_insight(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a published article by slug, or None if not found."""
    for article in list_published_insights(path):
        if article["slug"] == slug:
            return article
    return None


def _json_ld_safe(value: str) -> str:
    """Escape characters that could break out of a script block in JSON-LD."""
    return value.replace("<", "\\u003c").replace(">", "\\u003e")


def _render_head(
    *,
    title: str,
    description: str,
    canonical_path: str,
    og_type: str,
    json_ld: dict[str, Any],
    feed_link: bool = False,
) -> str:
    canonical = f"{CANONICAL_BASE}{canonical_path}"
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    canonical_esc = html.escape(canonical, quote=True)
    og_type_esc = html.escape(og_type)
    ld_json = json.dumps(json_ld, ensure_ascii=False)

    feed_tag = ""
    if feed_link:
        feed_tag = (
            '    <link rel="alternate" type="application/atom+xml" '
            f'title="saberistic insights" href="{CANONICAL_BASE}/insights/feed.xml" />\n'
        )

    return f"""    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title_esc}</title>
    <meta name="description" content="{desc_esc}" />
    <link rel="canonical" href="{canonical_esc}" />
{feed_tag}    <meta property="og:type" content="{og_type_esc}" />
    <meta property="og:site_name" content="saberistic" />
    <meta property="og:title" content="{title_esc}" />
    <meta property="og:description" content="{desc_esc}" />
    <meta property="og:url" content="{canonical_esc}" />
    <meta property="og:image" content="{OG_IMAGE}" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{title_esc}" />
    <meta name="twitter:description" content="{desc_esc}" />
    <meta name="twitter:image" content="{OG_IMAGE}" />
    <meta name="twitter:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />
    <script type="application/ld+json">
{ld_json}
    </script>
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />"""


def _render_page_shell(*, head: str, main: str, active_path: str | None = None) -> str:
    header = render_site_header(active_path)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
{head}
  </head>
  <body>
{header}

    <main>
{main}
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""


def _render_paragraphs(paragraphs: list[str]) -> str:
    return "\n".join(f"          <p>{html.escape(p)}</p>" for p in paragraphs)


def _render_sections(sections: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for section in sections:
        title = html.escape(section["title"])
        slug_id = html.escape(
            section["title"].lower().replace(" ", "-").replace(":", "")[:48],
            quote=False,
        )
        paras = _render_paragraphs(section["paragraphs"])
        blocks.append(
            f"""        <section class="insight-section" aria-labelledby="{slug_id}">
          <h2 class="insight-section-title" id="{slug_id}">{title}</h2>
{paras}
        </section>"""
        )
    return "\n".join(blocks)


def render_insights_index(path: Path | None = None) -> str:
    """Render the /insights listing page."""
    articles = list_published_insights(path)
    title = "Insights — saberistic"
    description = (
        "Architecture judgment for founders, investors, and technical leaders — "
        "fintech, digital assets, and high-stakes product delivery."
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "description": description,
        "url": f"{CANONICAL_BASE}/insights",
        "isPartOf": {"@type": "WebSite", "name": "saberistic", "url": f"{CANONICAL_BASE}/"},
    }

    head = _render_head(
        title=title,
        description=description,
        canonical_path="/insights",
        og_type="website",
        json_ld=json_ld,
        feed_link=True,
    )

    items = "\n".join(
        f"""          <li class="proof-item">
            <a class="proof-link" href="/insights/{html.escape(article['slug'])}">
              <span class="proof-headline">{html.escape(article['title'])}</span>
              <span class="proof-meta">{html.escape(article['published_at'])} · {html.escape(article['audience'])}</span>
            </a>
          </li>"""
        for article in articles
    )

    main = f"""      <section class="block insights-index" aria-labelledby="insights-title">
        <p class="eyebrow">Authority content</p>
        <h1 class="page-title" id="insights-title">Insights</h1>
        <p class="block-intro">
          Architecture judgment for qualified inbound leads — accurate, non-confidential,
          and scoped to problems founders and technical leaders actually face.
        </p>
        <ul class="proof-list">
{items}
        </ul>
        <p class="insights-feed">
          <a href="/insights/feed.xml">Atom feed</a>
        </p>
      </section>"""

    return _render_page_shell(head=head, main=main, active_path="/insights")


def render_insight_page(article: dict[str, Any]) -> str:
    """Render a full HTML page for one insight article."""
    slug = article["slug"]
    title = article["title"]
    page_title = f"{title} — saberistic"
    meta = article["meta_description"]
    canonical_path = f"/insights/{slug}"
    author = article.get("author", DEFAULT_AUTHOR)
    published = article["published_at"]

    json_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": _json_ld_safe(title),
        "description": _json_ld_safe(meta),
        "datePublished": published,
        "author": {"@type": "Person", "name": _json_ld_safe(author)},
        "publisher": {
            "@type": "Organization",
            "name": "saberistic",
            "url": f"{CANONICAL_BASE}/",
        },
        "mainEntityOfPage": f"{CANONICAL_BASE}{canonical_path}",
        "image": OG_IMAGE,
    }

    head = _render_head(
        title=page_title,
        description=meta,
        canonical_path=canonical_path,
        og_type="article",
        json_ld=json_ld,
        feed_link=True,
    )

    audience = html.escape(article["audience"])
    problem = html.escape(article["problem"])
    cta_label = html.escape(article["cta_label"])
    cta_href = html.escape(article["cta_href"], quote=True)
    title_esc = html.escape(title)

    intro = _render_paragraphs(article["paragraphs"])
    sections = _render_sections(article.get("sections", []))
    sections_block = f"\n{sections}" if sections else ""

    main = f"""      <article class="block insight-article" data-slug="{html.escape(slug)}">
        <p class="insight-eyebrow">{audience}</p>
        <h1 class="page-title insight-title">{title_esc}</h1>
        <p class="insight-meta">
          <time datetime="{html.escape(published)}">{html.escape(published)}</time>
          · {html.escape(author)}
        </p>
        <p class="insight-problem">{problem}</p>
        <div class="about-prose insight-body">
{intro}
        </div>{sections_block}
        <p class="insight-cta-row">
          <a class="cta" href="{cta_href}">{cta_label}</a>
        </p>
      </article>"""

    return _render_page_shell(head=head, main=main, active_path="/insights")


def render_atom_feed(path: Path | None = None) -> str:
    """Render an Atom feed for published insight articles."""
    articles = list_published_insights(path)
    entries = []
    for article in articles:
        slug = html.escape(article["slug"])
        link = f"{CANONICAL_BASE}/insights/{slug}"
        entries.append(
            f"""  <entry>
    <title>{html.escape(article['title'])}</title>
    <link href="{link}" />
    <id>{link}</id>
    <updated>{html.escape(article['published_at'])}T00:00:00Z</updated>
    <summary>{html.escape(article['excerpt'])}</summary>
    <author><name>{html.escape(article.get('author', DEFAULT_AUTHOR))}</name></author>
  </entry>"""
        )
    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>saberistic insights</title>
  <link href="{CANONICAL_BASE}/insights" />
  <link rel="self" href="{CANONICAL_BASE}/insights/feed.xml" />
  <id>{CANONICAL_BASE}/insights</id>
  <updated>{articles[0]['published_at'] if articles else '2026-07-13'}T00:00:00Z</updated>
{body}
</feed>
"""
