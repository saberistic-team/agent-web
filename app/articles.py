"""Article data and HTML rendering for /insights/{slug} authority content."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal

Audience = Literal["founders", "investors", "engineers", "leaders"]

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "articles.json"

CANONICAL_BASE = "https://saberistic.com"
OG_IMAGE = f"{CANONICAL_BASE}/assets/og-share.png"
OG_IMAGE_ALT = (
    "saberistic — AmirSaber Sharifi — filling gaps between markets and tech"
)
ATOM_FEED_PATH = "/insights/feed.xml"

AUDIENCE_LABELS: dict[str, str] = {
    "founders": "For founders",
    "investors": "For investors",
    "engineers": "For engineering leaders",
    "leaders": "For technical leaders",
}

REQUIRED_FIELDS = (
    "slug",
    "title",
    "meta_description",
    "audience",
    "published_date",
    "author",
    "problem",
    "cta_label",
    "cta_href",
)


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

        audience = item.get("audience")
        if audience not in AUDIENCE_LABELS:
            raise ValueError(f"invalid audience for {slug}: {audience!r}")

        for key in REQUIRED_FIELDS:
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"article {slug} missing or empty field: {key}")

        sections = item.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"article {slug} requires a non-empty sections array")
        for section in sections:
            if not isinstance(section, dict):
                raise ValueError(f"article {slug} sections must be objects")
            for sec_key in ("heading", "body"):
                if not isinstance(section.get(sec_key), str) or not section[sec_key].strip():
                    raise ValueError(f"article {slug} section missing {sec_key}")

        validated.append(item)
    return validated


def get_article(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a single article by slug, or None if not found."""
    for article in load_articles(path):
        if article["slug"] == slug:
            return article
    return None


def list_featured_slugs(path: Path | None = None) -> list[str]:
    """Slugs promoted on the homepage (first two articles)."""
    return [article["slug"] for article in load_articles(path)[:2]]


def _fonts_and_css() -> str:
    return """    <link rel="icon" href="/assets/logo.png" type="image/png" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=IBM+Plex+Mono:wght@400;500&display=swap"
      rel="stylesheet"
    />
    <link rel="stylesheet" href="/assets/site.css" />"""


def _social_meta(
    *,
    title: str,
    description: str,
    url: str,
    og_type: str = "website",
) -> str:
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    url_esc = html.escape(url, quote=True)
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


def _header() -> str:
    return """    <header class="top">
      <a class="brand" href="/" aria-label="saberistic home">
        <img
          class="brand-word"
          src="/assets/logo-text.png"
          width="160"
          height="41"
          alt="saberistic"
        />
      </a>
      <a class="top-link" href="/insights">Insights</a>
    </header>"""


def _footer() -> str:
    return """    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
      <p><a href="/insights">Insights</a></p>
    </footer>"""


def render_article_page(article: dict[str, Any]) -> str:
    """Render a full HTML page for one article."""
    slug = html.escape(article["slug"])
    title = html.escape(article["title"])
    meta = html.escape(article["meta_description"])
    audience = article["audience"]
    audience_label = html.escape(AUDIENCE_LABELS[audience])  # type: ignore[index]
    author = html.escape(article["author"])
    published = html.escape(article["published_date"])
    problem = html.escape(article["problem"])
    cta_label = html.escape(article["cta_label"])
    cta_href = html.escape(article["cta_href"], quote=True)
    page_title = f"{title} · saberistic"
    canonical = f"{CANONICAL_BASE}/insights/{article['slug']}"

    sections_html = "\n".join(
        f"""          <section class="article-section" aria-labelledby="section-{i}-title">
            <h2 class="article-section-title" id="section-{i}-title">{html.escape(section['heading'])}</h2>
            <p>{html.escape(section['body'])}</p>
          </section>"""
        for i, section in enumerate(article["sections"])
    )

    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": article["title"],
            "description": article["meta_description"],
            "url": canonical,
            "datePublished": article["published_date"],
            "author": {
                "@type": "Person",
                "name": article["author"],
                "url": f"{CANONICAL_BASE}/about",
            },
            "publisher": {
                "@type": "Organization",
                "name": "saberistic",
                "url": CANONICAL_BASE,
            },
        },
        indent=2,
    ).replace("<", "\\u003c")

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{page_title}</title>
    <meta name="description" content="{meta}" />
    <link rel="canonical" href="{html.escape(canonical, quote=True)}" />
{_social_meta(title=article["title"], description=article["meta_description"], url=canonical, og_type="article")}
    <script type="application/ld+json">
{json_ld}
    </script>
{_fonts_and_css()}
  </head>
  <body>
{_header()}

    <main>
      <article class="block article" data-slug="{slug}" data-audience="{html.escape(audience)}">
        <p class="article-eyebrow">{audience_label}</p>
        <h1 class="page-title article-title">{title}</h1>
        <p class="article-meta">
          <span class="article-author">{author}</span>
          <span class="article-date" datetime="{published}">{published}</span>
        </p>
        <p class="article-problem">{problem}</p>
{sections_html}
        <p class="article-cta-row">
          <a class="cta" href="{cta_href}">{cta_label}</a>
        </p>
      </article>
    </main>

{_footer()}
  </body>
</html>
"""


def render_insights_index_page(articles: list[dict[str, Any]] | None = None) -> str:
    """Render the /insights listing page."""
    items = articles if articles is not None else load_articles()
    title = "Insights — saberistic"
    description = (
        "Architecture judgment for founders, investors, and technical leaders — "
        "from AmirSaber Sharifi and saberistic."
    )
    canonical = f"{CANONICAL_BASE}/insights"

    list_html = "\n".join(
        f"""          <li class="article-item">
            <a class="article-link" href="/insights/{html.escape(article['slug'])}">
              <span class="article-link-title">{html.escape(article['title'])}</span>
              <span class="article-link-meta">{html.escape(AUDIENCE_LABELS[article['audience']])} · {html.escape(article['published_date'])}</span>
            </a>
          </li>"""
        for article in items
    )

    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Insights",
            "description": description,
            "url": canonical,
        },
        indent=2,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)}</title>
    <meta name="description" content="{html.escape(description)}" />
    <link rel="canonical" href="{html.escape(canonical, quote=True)}" />
    <link rel="alternate" type="application/atom+xml" title="saberistic insights" href="{ATOM_FEED_PATH}" />
{_social_meta(title=title, description=description, url=canonical)}
    <script type="application/ld+json">
{json_ld}
    </script>
{_fonts_and_css()}
  </head>
  <body>
{_header()}

    <main>
      <section class="block" aria-labelledby="insights-title">
        <h1 class="page-title" id="insights-title">Insights</h1>
        <p class="block-intro">
          Architecture judgment for qualified inbound leads — accurate, non-confidential
          perspectives on fintech, digital assets, and scaling systems.
        </p>
        <ul class="article-list">
{list_html}
        </ul>
      </section>
    </main>

{_footer()}
  </body>
</html>
"""


def render_atom_feed(articles: list[dict[str, Any]] | None = None) -> str:
    """Render an Atom feed for published articles."""
    items = articles if articles is not None else load_articles()
    entries = []
    for article in items:
        slug = article["slug"]
        url = f"{CANONICAL_BASE}/insights/{slug}"
        entries.append(
            "  <entry>\n"
            f"    <title>{html.escape(article['title'])}</title>\n"
            f"    <link href=\"{html.escape(url, quote=True)}\" />\n"
            f"    <id>{html.escape(url, quote=True)}</id>\n"
            f"    <updated>{html.escape(article['published_date'])}T00:00:00Z</updated>\n"
            f"    <summary>{html.escape(article['meta_description'])}</summary>\n"
            f"    <author><name>{html.escape(article['author'])}</name></author>\n"
            "  </entry>"
        )
    feed_entries = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>saberistic insights</title>\n"
        f"  <link href=\"{CANONICAL_BASE}/insights\" />\n"
        f"  <link rel=\"self\" href=\"{CANONICAL_BASE}{ATOM_FEED_PATH}\" />\n"
        f"  <id>{CANONICAL_BASE}/insights</id>\n"
        f"  <updated>{html.escape(items[0]['published_date'])}T00:00:00Z</updated>\n"
        f"{feed_entries}\n"
        "</feed>\n"
    )
