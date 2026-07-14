"""Shared HTML head metadata for dynamic pages (Open Graph, Twitter, JSON-LD)."""

from __future__ import annotations

import html
import json
from typing import Any

from app.seo import CANONICAL_BASE

OG_IMAGE = f"{CANONICAL_BASE}/assets/og-share.png"
OG_IMAGE_ALT = "saberistic — high-stakes architecture and engineering leadership"


def _json_ld_safe(value: str) -> str:
    """Escape characters that could break out of a script block in JSON-LD."""
    return value.replace("<", "\\u003c").replace(">", "\\u003e")


def social_meta_tags(
    *,
    title: str,
    description: str,
    url: str,
    og_type: str = "website",
) -> str:
    """Return Open Graph and Twitter card meta tags."""
    t = html.escape(title)
    d = html.escape(description)
    u = html.escape(url, quote=True)
    return f"""    <meta property="og:type" content="{html.escape(og_type)}" />
    <meta property="og:site_name" content="saberistic" />
    <meta property="og:title" content="{t}" />
    <meta property="og:description" content="{d}" />
    <meta property="og:url" content="{u}" />
    <meta property="og:image" content="{OG_IMAGE}" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{t}" />
    <meta name="twitter:description" content="{d}" />
    <meta name="twitter:image" content="{OG_IMAGE}" />
    <meta name="twitter:image:alt" content="{html.escape(OG_IMAGE_ALT)}" />"""


def json_ld_script(data: dict[str, Any]) -> str:
    """Return a JSON-LD script tag safe to embed in HTML."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return f'    <script type="application/ld+json">\n      {payload}\n    </script>'


def web_page_json_ld(*, title: str, description: str, url: str) -> str:
    return json_ld_script(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": title,
            "description": description,
            "url": url,
        }
    )


def case_study_page_json_ld(*, title: str, description: str, url: str) -> dict[str, Any]:
    """Return JSON-LD for a /work/{slug} proof page (WebPage, not Article)."""
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": _json_ld_safe(title),
        "description": _json_ld_safe(description),
        "url": url,
        "image": OG_IMAGE,
        "isPartOf": {"@type": "WebSite", "name": "saberistic", "url": f"{CANONICAL_BASE}/"},
    }


def article_json_ld(
    *,
    title: str,
    description: str,
    url: str,
    author: str,
    date_published: str,
    date_modified: str | None = None,
) -> str:
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": description,
        "url": url,
        "author": {"@type": "Person", "name": author},
        "publisher": {
            "@type": "Organization",
            "name": "saberistic",
            "url": f"{CANONICAL_BASE}/",
        },
        "datePublished": date_published,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
    }
    if date_modified:
        data["dateModified"] = date_modified
    return json_ld_script(data)
