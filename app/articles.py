"""Article data and HTML rendering for /insights and /insights/{slug} pages."""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from app.seo import CANONICAL_BASE, canonical_url

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "articles.json"

OG_IMAGE = f"{CANONICAL_BASE}/assets/og-share.png"
OG_IMAGE_ALT = (
    "saberistic — AmirSaber Sharifi — filling gaps between markets and tech"
)

REQUIRED_FIELDS = (
    "slug",
    "title",
    "audience",
    "problem",
    "meta_description",
    "author",
    "published_date",
    "cta_label",
    "cta_href",
)


def load_articles(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate articles from JSON."""
    source = path or DATA_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    items = raw.get("articles")
    if not isinstance(items, list) or not items:
        raise ValueError("articles.json must contain a non-empty articles array")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each article must be an object")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("each article requires a slug")
        if slug in seen:
            raise ValueError(f"duplicate article slug: {slug}")
        seen.add(slug)

        published = item.get("published")
        if not isinstance(published, bool):
            raise ValueError(f"article {slug} requires published: true|false")

        for key in REQUIRED_FIELDS:
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"article {slug} missing or empty field: {key}")

        sections = item.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"article {slug} requires a non-empty sections array")
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"article {slug} sections must be objects")
            heading = section.get("heading")
            paragraphs = section.get("paragraphs")
            if not isinstance(heading, str) or not heading.strip():
                raise ValueError(f"article {slug} section missing heading")
            if not isinstance(paragraphs, list) or not paragraphs:
                raise ValueError(f"article {slug} section {heading!r} needs paragraphs")
            if not all(isinstance(p, str) and p.strip() for p in paragraphs):
                raise ValueError(f"article {slug} section {heading!r} has invalid paragraphs")

        validated.append(item)
    return validated


def list_published_articles(path: Path | None = None) -> list[dict[str, Any]]:
    """Return published articles sorted newest first."""
    published = [article for article in load_articles(path) if article["published"]]
    return sorted(published, key=lambda item: item["published_date"], reverse=True)


def get_article(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a published article by slug, or None if missing or draft."""
    for article in load_articles(path):
        if article["slug"] == slug and article["published"]:
            return article
    return None


def _format_display_date(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return parsed.strftime("%d %b %Y")


def _social_meta_tags(
    *,
    title: str,
    description: str,
    page_url: str,
    og_type: str = "website",
) -> str:
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    url_esc = html.escape(page_url)
    return f"""    <meta property="og:type" content="{html.escape(og_type)}" />
    <meta property="og:site_name" content="saberistic" />
    <meta property="og:title" content="{title_esc}" />
    <meta property="og:description" content="{desc_esc}" />
    <meta property="og:url" content="{url_esc}" />
    <meta property="og:image" content="{OG_IMAGE}" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{title_esc}" />
    <meta name="twitter:description" content="{desc_esc}" />
    <meta name="twitter:image" content="{OG_IMAGE}" />
    <meta name="twitter:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />"""


def _page_head(
    *,
    title: str,
    description: str,
    canonical: str,
    og_type: str = "website",
    json_ld: str,
) -> str:
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    canonical_esc = html.escape(canonical)
    return f"""  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title_esc}</title>
    <meta name="description" content="{desc_esc}" />
    <link rel="canonical" href="{canonical_esc}" />
    <link
      rel="alternate"
      type="application/atom+xml"
      title="saberistic insights"
      href="{canonical_url('/insights/feed.xml')}"
    />
{_social_meta_tags(title=title, description=description, page_url=canonical, og_type=og_type)}
    <script type="application/ld+json">
{json_ld}
    </script>
    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />
  </head>"""


def _site_header(*, back_href: str, back_label: str) -> str:
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
      <a class="top-link" href="{html.escape(back_href, quote=True)}">{html.escape(back_label)}</a>
    </header>"""


def _site_footer() -> str:
    return """    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
      <nav class="foot-nav" aria-label="Site">
        <a href="/">Home</a>
        <a href="/insights">Insights</a>
        <a href="/about">About</a>
        <a href="/brief">Brief</a>
      </nav>
    </footer>"""


def _render_sections(sections: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for section in sections:
        heading = html.escape(section["heading"])
        slug = heading.lower().replace(" ", "-")
        paragraphs = "\n".join(
            f"            <p>{html.escape(paragraph)}</p>"
            for paragraph in section["paragraphs"]
        )
        blocks.append(
            f"""          <section class="article-section" aria-labelledby="{slug}">
            <h2 class="article-section-title" id="{slug}">{heading}</h2>
{paragraphs}
          </section>"""
        )
    return "\n".join(blocks)


def render_article_page(article: dict[str, Any]) -> str:
    """Render a full HTML page for one published article."""
    slug = article["slug"]
    title = article["title"]
    audience = html.escape(article["audience"])
    problem = html.escape(article["problem"])
    meta = article["meta_description"]
    author = html.escape(article["author"])
    display_date = _format_display_date(article["published_date"])
    cta_label = html.escape(article["cta_label"])
    cta_href = html.escape(article["cta_href"], quote=True)
    page_title = f"{title} · saberistic"
    page_url = canonical_url(f"/insights/{slug}")

    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": title,
            "description": meta,
            "author": {"@type": "Person", "name": article["author"]},
            "datePublished": article["published_date"],
            "publisher": {
                "@type": "Organization",
                "name": "saberistic",
                "url": f"{CANONICAL_BASE}/",
            },
            "mainEntityOfPage": page_url,
            "url": page_url,
        },
        indent=2,
    ).replace("<", "\\u003c")

    return f"""<!DOCTYPE html>
<html lang="en">
{_page_head(title=page_title, description=meta, canonical=page_url, og_type="article", json_ld=json_ld)}
  <body>
{_site_header(back_href="/insights", back_label="Insights")}
    <main>
      <article class="block article" itemscope itemtype="https://schema.org/Article">
        <meta itemprop="datePublished" content="{html.escape(article['published_date'])}" />
        <p class="article-eyebrow">{audience}</p>
        <h1 class="page-title article-title" itemprop="headline">{html.escape(title)}</h1>
        <p class="article-meta">
          <span itemprop="author" itemscope itemtype="https://schema.org/Person">
            <span itemprop="name">{author}</span>
          </span>
          · <time datetime="{html.escape(article['published_date'])}">{display_date}</time>
        </p>
        <p class="article-lede">{problem}</p>
{_render_sections(article["sections"])}
        <p class="article-cta-row">
          <a class="cta" href="{cta_href}">{cta_label}</a>
          <a class="cta cta-secondary" href="/insights">All insights</a>
        </p>
      </article>
    </main>
{_site_footer()}
  </body>
</html>
"""


def render_insights_index(articles: list[dict[str, Any]] | None = None) -> str:
    """Render the /insights listing page."""
    items = articles if articles is not None else list_published_articles()
    title = "Insights — saberistic"
    description = (
        "Architecture judgment for founders, investors, and technical leaders — "
        "patterns, risks, and decisions from seed–Series B fintech and digital-asset systems."
    )
    page_url = canonical_url("/insights")

    list_items: list[str] = []
    for article in items:
        slug = html.escape(article["slug"])
        item_title = html.escape(article["title"])
        audience = html.escape(article["audience"])
        problem = html.escape(article["problem"])
        display_date = _format_display_date(article["published_date"])
        list_items.append(
            f"""          <li class="insights-item">
            <a class="insights-link" href="/insights/{slug}">
              <span class="insights-headline">{item_title}</span>
              <span class="insights-meta">{audience} · {display_date}</span>
              <span class="insights-lede">{problem}</span>
            </a>
          </li>"""
        )
    articles_html = "\n".join(list_items) if list_items else (
        '          <li class="insights-empty">No published articles yet.</li>'
    )

    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Insights",
            "description": description,
            "url": page_url,
        },
        indent=2,
    ).replace("<", "\\u003c")

    return f"""<!DOCTYPE html>
<html lang="en">
{_page_head(title=title, description=description, canonical=page_url, json_ld=json_ld)}
  <body>
{_site_header(back_href="/", back_label="Home")}
    <main>
      <section class="block insights-index" aria-labelledby="insights-title">
        <h1 class="page-title" id="insights-title">Insights</h1>
        <p class="insights-intro">
          Architecture judgment for qualified inbound leads — clear audience, real
          problems, and decisions that survive diligence.
        </p>
        <ul class="insights-list">
{articles_html}
        </ul>
        <p class="article-cta-row">
          <a class="cta" href="/brief">Request architecture review</a>
          <a class="cta cta-secondary" href="/about">About saberistic</a>
        </p>
      </section>
    </main>
{_site_footer()}
  </body>
</html>
"""


def atom_feed_xml(articles: list[dict[str, Any]] | None = None) -> str:
    """Render an Atom feed for published insights."""
    items = articles if articles is not None else list_published_articles()
    feed_url = canonical_url("/insights/feed.xml")
    index_url = canonical_url("/insights")
    updated = items[0]["published_date"] if items else date.today().isoformat()

    entries: list[str] = []
    for article in items:
        slug = article["slug"]
        entry_url = canonical_url(f"/insights/{slug}")
        entries.append(
            "  <entry>\n"
            f"    <title>{xml_escape(article['title'])}</title>\n"
            f"    <link href=\"{xml_escape(entry_url)}\" />\n"
            f"    <id>{xml_escape(entry_url)}</id>\n"
            f"    <updated>{xml_escape(article['published_date'])}T00:00:00Z</updated>\n"
            f"    <summary>{xml_escape(article['problem'])}</summary>\n"
            f"    <author><name>{xml_escape(article['author'])}</name></author>\n"
            "  </entry>"
        )

    entry_block = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>saberistic insights</title>\n"
        f"  <link href=\"{xml_escape(index_url)}\" />\n"
        f"  <link href=\"{xml_escape(feed_url)}\" rel=\"self\" />\n"
        f"  <id>{xml_escape(index_url)}</id>\n"
        f"  <updated>{xml_escape(updated)}T00:00:00Z</updated>\n"
        f"{entry_block}\n"
        "</feed>\n"
    )
