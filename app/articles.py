"""Lightweight authority-content system for /insights articles."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.seo import CANONICAL_BASE

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = REPO_ROOT / "articles"
TEMPLATES_DIR = REPO_ROOT / "site" / "templates"
SITE_NAME = "saberistic"
AUTHOR_NAME = "AmirSaber Sharifi"


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    description: str
    audience: str
    problem: str
    published: date
    updated: date
    cta_text: str
    cta_href: str
    body_file: str

    @property
    def path(self) -> str:
        return f"/insights/{self.slug}"

    def canonical_url(self) -> str:
        return f"{CANONICAL_BASE}{self.path}"

    def body_html(self) -> str:
        path = ARTICLES_DIR / self.body_file
        if not path.is_file():
            raise FileNotFoundError(f"article body missing: {path}")
        return path.read_text(encoding="utf-8").strip()


ARTICLES: tuple[Article, ...] = (
    Article(
        slug="competing-sources-of-truth",
        title="Five signs an MVP has competing sources of truth",
        description=(
            "How founders and technical leaders spot fragmented data models "
            "before they become expensive product and compliance problems."
        ),
        audience="Founders and technical leaders shipping an MVP",
        problem=(
            "The product looks fine in demos, but dashboards, support tools, "
            "and customer-facing balances disagree."
        ),
        published=date(2026, 7, 8),
        updated=date(2026, 7, 8),
        cta_text="Request an architecture review",
        cta_href="/brief",
        body_file="competing-sources-of-truth.html",
    ),
    Article(
        slug="fintech-architecture-due-diligence",
        title="What investors should examine before funding fintech architecture",
        description=(
            "A practical due-diligence checklist for investors evaluating "
            "whether a fintech startup can scale without hidden rebuild cost."
        ),
        audience="Investors and technical advisors evaluating fintech startups",
        problem=(
            "Pitch decks show traction, but the ledger, custody, and reporting "
            "layers may not survive the next funding round."
        ),
        published=date(2026, 7, 10),
        updated=date(2026, 7, 10),
        cta_text="Discuss technical diligence",
        cta_href="/brief",
        body_file="fintech-architecture-due-diligence.html",
    ),
)

_ARTICLES_BY_SLUG = {article.slug: article for article in ARTICLES}


def list_articles() -> list[Article]:
    return sorted(ARTICLES, key=lambda article: article.published, reverse=True)


def get_article(slug: str) -> Article | None:
    return _ARTICLES_BY_SLUG.get(slug)


def _render_template(name: str, **context: str) -> str:
    template = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    for key, value in context.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    if re.search(r"\{\{[a-z_]+\}\}", template):
        raise ValueError(f"unresolved template placeholders in {name}")
    return template


def _layout(
    *,
    title: str,
    description: str,
    canonical_path: str,
    head_extra: str,
    main: str,
) -> str:
    canonical_url = f"{CANONICAL_BASE}{canonical_path}" if canonical_path != "/" else f"{CANONICAL_BASE}/"
    return _render_template(
        "layout.html",
        title=html.escape(title),
        description=html.escape(description),
        canonical_url=html.escape(canonical_url),
        head_extra=head_extra,
        main=main,
        insights_href="/insights",
    )


def _format_article_date(value: date) -> str:
    return value.strftime("%B %d, %Y")


def _article_json_ld(article: Article) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article.title,
        "description": article.description,
        "author": {"@type": "Person", "name": AUTHOR_NAME},
        "publisher": {"@type": "Organization", "name": SITE_NAME},
        "datePublished": article.published.isoformat(),
        "dateModified": article.updated.isoformat(),
        "mainEntityOfPage": article.canonical_url(),
    }
    serialized = json.dumps(payload, ensure_ascii=True)
    return f'<script type="application/ld+json">{serialized}</script>'


def _article_head_extra(article: Article) -> str:
    canonical = article.canonical_url()
    title = html.escape(article.title)
    description = html.escape(article.description)
    json_ld = _article_json_ld(article)
    return (
        f'<meta property="og:type" content="article" />\n'
        f'    <meta property="og:title" content="{title}" />\n'
        f'    <meta property="og:description" content="{description}" />\n'
        f'    <meta property="og:url" content="{html.escape(canonical)}" />\n'
        f'    <meta name="twitter:card" content="summary" />\n'
        f'    <meta name="twitter:title" content="{title}" />\n'
        f'    <meta name="twitter:description" content="{description}" />\n'
        f"    {json_ld}"
    )


def render_insights_index() -> str:
    rows = []
    for article in list_articles():
        rows.append(
            _render_template(
                "insights_item.html",
                href=html.escape(article.path),
                title=html.escape(article.title),
                audience=html.escape(article.audience),
                date=html.escape(_format_article_date(article.published)),
                description=html.escape(article.description),
            )
        )
    main = _render_template(
        "insights_index.html",
        article_rows="\n".join(rows),
    )
    return _layout(
        title=f"{AUTHOR_NAME} — Insights",
        description=(
            "Architecture judgment for founders, investors, and technical "
            "leaders — essays on fintech systems, data integrity, and security."
        ),
        canonical_path="/insights",
        head_extra=(
            '<meta property="og:type" content="website" />\n'
            '    <meta name="twitter:card" content="summary" />'
        ),
        main=main,
    )


def render_article_page(article: Article) -> str:
    main = _render_template(
        "article.html",
        title=html.escape(article.title),
        audience=html.escape(article.audience),
        problem=html.escape(article.problem),
        published=html.escape(_format_article_date(article.published)),
        published_iso=article.published.isoformat(),
        updated=html.escape(_format_article_date(article.updated)),
        updated_iso=article.updated.isoformat(),
        author=html.escape(AUTHOR_NAME),
        body=article.body_html(),
        cta_text=html.escape(article.cta_text),
        cta_href=html.escape(article.cta_href),
    )
    page_title = f"{article.title} — {AUTHOR_NAME}"
    return _layout(
        title=page_title,
        description=article.description,
        canonical_path=article.path,
        head_extra=_article_head_extra(article),
        main=main,
    )


def render_atom_feed() -> str:
    feed_id = f"{CANONICAL_BASE}/insights"
    entries = []
    for article in list_articles():
        entries.append(
            _render_template(
                "atom_entry.xml",
                title=html.escape(article.title),
                id=html.escape(article.canonical_url()),
                updated=article.updated.isoformat(),
                link=html.escape(article.canonical_url()),
                summary=html.escape(article.description),
            )
        )
    return _render_template(
        "atom_feed.xml",
        feed_id=html.escape(feed_id),
        updated=max(article.updated for article in ARTICLES).isoformat(),
        title=html.escape(f"{SITE_NAME} insights"),
        entries="\n".join(entries),
    )
