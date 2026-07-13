"""Insight article data and HTML rendering for /insights pages."""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.seo import CANONICAL_BASE

Status = Literal["published", "draft"]

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "insights.json"

OG_IMAGE = f"{CANONICAL_BASE}/assets/og-share.png"
OG_IMAGE_ALT = (
    "saberistic — AmirSaber Sharifi — filling gaps between markets and tech"
)
DEFAULT_AUTHOR = "AmirSaber Sharifi"


def load_articles(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate all insight articles from JSON."""
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

        status = item.get("status")
        if status not in ("published", "draft"):
            raise ValueError(f"invalid status for {slug}: {status!r}")

        published = item.get("published")
        if not isinstance(published, str) or not published.strip():
            raise ValueError(f"article {slug} missing published date")
        try:
            date.fromisoformat(published)
        except ValueError as exc:
            raise ValueError(f"article {slug} has invalid published date") from exc

        for key in (
            "title",
            "eyebrow",
            "audience",
            "problem",
            "meta_description",
            "summary",
            "cta_label",
            "cta_href",
        ):
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"article {slug} missing or empty field: {key}")

        author = item.get("author")
        if author is not None and (not isinstance(author, str) or not author.strip()):
            raise ValueError(f"article {slug} has invalid author")

        sections = item.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"article {slug} requires a non-empty sections array")
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"article {slug} sections must be objects")
            for key in ("heading", "body"):
                if not isinstance(section.get(key), str) or not str(section[key]).strip():
                    raise ValueError(f"article {slug} section missing {key}")

        validated.append(item)
    return validated


def load_published_articles(path: Path | None = None) -> list[dict[str, Any]]:
    """Return published articles sorted newest first."""
    published = [a for a in load_articles(path) if a["status"] == "published"]
    return sorted(published, key=lambda a: a["published"], reverse=True)


def get_article(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a published article by slug, or None if missing or draft."""
    for article in load_articles(path):
        if article["slug"] == slug and article["status"] == "published":
            return article
    return None


def _safe_ld_json(data: dict[str, Any]) -> str:
    """Serialize JSON-LD without HTML-breakout sequences in script tags."""
    return json.dumps(data, indent=2).replace("<", "\\u003c").replace(">", "\\u003e")


def article_author(article: dict[str, Any]) -> str:
    return str(article.get("author") or DEFAULT_AUTHOR)


def article_path(slug: str) -> str:
    return f"/insights/{slug}"


def article_canonical(slug: str) -> str:
    return f"{CANONICAL_BASE}{article_path(slug)}"


def _social_head(
    *,
    title: str,
    description: str,
    canonical: str,
    og_type: str,
    ld_json: str,
) -> str:
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    canonical_esc = html.escape(canonical, quote=True)
    return f"""    <title>{title_esc}</title>
    <meta name="description" content="{desc_esc}" />
    <link rel="canonical" href="{canonical_esc}" />
    <meta property="og:type" content="{html.escape(og_type)}" />
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
    </script>"""


def _page_shell(
    *,
    title: str,
    description: str,
    canonical: str,
    og_type: str,
    ld_json: str,
    body: str,
    top_link_label: str = "Home",
    top_link_href: str = "/",
) -> str:
    head = _social_head(
        title=title,
        description=description,
        canonical=canonical,
        og_type=og_type,
        ld_json=ld_json,
    )
    top_href = html.escape(top_link_href, quote=True)
    top_label = html.escape(top_link_label)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
{head}
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="alternate" type="application/atom+xml" title="saberistic insights" href="/insights/feed.xml" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>
  <body>
    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
      <a class="top-link" href="{top_href}">{top_label}</a>
    </header>

    <main>
{body}
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""


def render_insights_index(path: Path | None = None) -> str:
    """Render the /insights listing page."""
    articles = load_published_articles(path)
    title = "Insights — saberistic"
    description = (
        "Architecture judgment for founders, investors, and technical leaders — "
        "patterns, risks, and decisions from fintech, AI, and digital-asset systems."
    )
    canonical = f"{CANONICAL_BASE}/insights"

    items_html = "\n".join(
        f"""          <li class="proof-item">
            <a class="proof-link" href="{html.escape(article_path(article['slug']), quote=True)}">
              <span class="proof-headline">{html.escape(article['title'])}</span>
              <span class="proof-meta">{html.escape(article['eyebrow'])} · {html.escape(article['published'])}</span>
            </a>
          </li>"""
        for article in articles
    )

    ld_json = _safe_ld_json(
        {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Insights",
            "url": canonical,
            "description": description,
            "isPartOf": {"@id": f"{CANONICAL_BASE}/#organization"},
        }
    )

    body = f"""      <section class="block insights-index" aria-labelledby="insights-title">
        <p class="case-eyebrow">Authority content</p>
        <h1 class="page-title" id="insights-title">Insights</h1>
        <p class="proof-lede">
          Architecture judgment for qualified inbound leads — clear audience,
          concrete problems, and actionable framing. No client names; no
          confidential employer details.
        </p>
        <ul class="proof-list">
{items_html}
        </ul>
        <p class="case-cta-row">
          <a class="cta" href="/brief">Architecture Diagnostic — $200</a>
          <a class="cta cta-secondary" href="/#proof">View proof</a>
        </p>
      </section>"""

    return _page_shell(
        title=title,
        description=description,
        canonical=canonical,
        og_type="website",
        ld_json=ld_json,
        body=body,
    )


def render_insight_page(article: dict[str, Any]) -> str:
    """Render a full HTML page for one published insight."""
    slug = article["slug"]
    title_text = f"{article['title']} — saberistic"
    meta = article["meta_description"]
    canonical = article_canonical(slug)
    author = article_author(article)
    published = article["published"]

    sections_html = "\n".join(
        f"""          <section class="insight-section" aria-labelledby="section-{index}-title">
            <h2 class="case-section-title" id="section-{index}-title">{html.escape(section['heading'])}</h2>
            <p>{html.escape(section['body'])}</p>
          </section>"""
        for index, section in enumerate(article["sections"])
    )

    cta_label = html.escape(article["cta_label"])
    cta_href = html.escape(article["cta_href"], quote=True)

    ld_json = _safe_ld_json(
        {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": article["title"],
            "description": meta,
            "datePublished": published,
            "author": {
                "@type": "Person",
                "name": author,
                "url": f"{CANONICAL_BASE}/about",
            },
            "publisher": {
                "@type": "Organization",
                "name": "saberistic",
                "url": CANONICAL_BASE,
            },
            "mainEntityOfPage": canonical,
            "url": canonical,
        }
    )

    body = f"""      <article class="block insight-article" data-slug="{html.escape(slug)}">
        <p class="case-eyebrow">{html.escape(article['eyebrow'])} · {html.escape(published)}</p>
        <h1 class="page-title case-title">{html.escape(article['title'])}</h1>
        <p class="insight-audience"><strong>Audience:</strong> {html.escape(article['audience'])}</p>
        <p class="insight-problem"><strong>Problem:</strong> {html.escape(article['problem'])}</p>
{sections_html}
        <p class="case-cta-row">
          <a class="cta" href="{cta_href}">{cta_label}</a>
          <a class="cta cta-secondary" href="/insights">All insights</a>
        </p>
      </article>"""

    return _page_shell(
        title=title_text,
        description=meta,
        canonical=canonical,
        og_type="article",
        ld_json=ld_json,
        body=body,
        top_link_label="Insights",
        top_link_href="/insights",
    )


def atom_feed_xml(path: Path | None = None) -> str:
    """Render an Atom feed for published insights."""
    articles = load_published_articles(path)
    updated = articles[0]["published"] if articles else date.today().isoformat()
    if articles:
        updated_dt = datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)
        updated_iso = updated_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        updated_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = []
    for article in articles:
        slug = article["slug"]
        link = article_canonical(slug)
        title = html.escape(article["title"])
        summary = html.escape(article["summary"])
        published_dt = datetime.fromisoformat(article["published"]).replace(tzinfo=timezone.utc)
        published_iso = published_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        entries.append(
            "  <entry>\n"
            f"    <title>{title}</title>\n"
            f'    <link href="{link}" />\n'
            f"    <id>{link}</id>\n"
            f"    <updated>{published_iso}</updated>\n"
            f"    <published>{published_iso}</published>\n"
            f"    <summary>{summary}</summary>\n"
            f"    <author><name>{html.escape(article_author(article))}</name></author>\n"
            "  </entry>"
        )

    entries_block = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>saberistic insights</title>\n"
        f'  <link href="{CANONICAL_BASE}/insights" />\n'
        f'  <link rel="self" href="{CANONICAL_BASE}/insights/feed.xml" />\n'
        f"  <id>{CANONICAL_BASE}/insights</id>\n"
        f"  <updated>{updated_iso}</updated>\n"
        f"{entries_block}\n"
        "</feed>\n"
    )


def lastmod_for_path(path: str, path_data: Path | None = None) -> date:
    """Return lastmod date for sitemap entries (article publish dates when known)."""
    prefix = "/insights/"
    if path.startswith(prefix):
        slug = path[len(prefix) :]
        article = get_article(slug, path=path_data)
        if article is not None:
            return date.fromisoformat(article["published"])
    return date.today()
