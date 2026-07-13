"""Case study data and HTML rendering for /work/{slug} pages."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal

from app.metadata import social_meta_tags, web_page_json_ld
from app.seo import CANONICAL_BASE

Engagement = Literal["employer", "founder", "saberistic"]

DATA_PATH = Path(__file__).resolve().parent.parent / "site" / "data" / "case-studies.json"

SECTIONS = (
    ("context", "Context"),
    ("problem", "Problem"),
    ("role", "Role"),
    ("intervention", "Intervention"),
    ("result", "Result"),
)

DISCLAIMERS: dict[Engagement, str] = {
    "employer": "Prior employer role — not a Saberistic client engagement.",
    "founder": "Independent venture — not a Saberistic client engagement.",
    "saberistic": "Saberistic engagement — sanitized composite; no client identified.",
}


def load_case_studies(path: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate case studies from JSON."""
    source = path or DATA_PATH
    raw = json.loads(source.read_text(encoding="utf-8"))
    studies = raw.get("studies")
    if not isinstance(studies, list) or not studies:
        raise ValueError("case-studies.json must contain a non-empty studies array")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in studies:
        if not isinstance(item, dict):
            raise ValueError("each study must be an object")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("each study requires a slug")
        if slug in seen:
            raise ValueError(f"duplicate case study slug: {slug}")
        seen.add(slug)

        engagement = item.get("engagement")
        if engagement not in DISCLAIMERS:
            raise ValueError(f"invalid engagement for {slug}: {engagement!r}")

        for key in (
            "org",
            "headline",
            "meta_description",
            "context",
            "problem",
            "role",
            "intervention",
            "result",
            "cta_label",
            "cta_href",
        ):
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"study {slug} missing or empty field: {key}")

        validated.append(item)
    return validated


def get_case_study(slug: str, path: Path | None = None) -> dict[str, Any] | None:
    """Return a single case study by slug, or None if not found."""
    for study in load_case_studies(path):
        if study["slug"] == slug:
            return study
    return None


def list_featured_slugs(path: Path | None = None) -> list[str]:
    """Slugs promoted on the homepage (first three studies)."""
    return [study["slug"] for study in load_case_studies(path)[:3]]


def case_study_page_title(study: dict[str, Any]) -> str:
    """Full document title for a case study page."""
    return f"{study['org']} — {study['headline']} · saberistic"


def case_study_canonical_url(study: dict[str, Any]) -> str:
    """Canonical URL for a case study page."""
    return f"{CANONICAL_BASE}/work/{study['slug']}"


def case_study_head_metadata(study: dict[str, Any]) -> str:
    """Open Graph, Twitter, and JSON-LD metadata for a case study page."""
    title = case_study_page_title(study)
    description = study["meta_description"]
    url = case_study_canonical_url(study)
    social = social_meta_tags(
        title=title,
        description=description,
        url=url,
        og_type="website",
    )
    ld = web_page_json_ld(title=title, description=description, url=url)
    return f"{social}\n{ld}"


def render_case_study_page(study: dict[str, Any]) -> str:
    """Render a full HTML page for one case study."""
    slug = html.escape(study["slug"])
    org = html.escape(study["org"])
    headline = html.escape(study["headline"])
    page_title = html.escape(case_study_page_title(study))
    meta = html.escape(study["meta_description"])
    canonical = html.escape(case_study_canonical_url(study), quote=True)
    head_metadata = case_study_head_metadata(study)
    engagement = study["engagement"]
    disclaimer = html.escape(DISCLAIMERS[engagement])  # type: ignore[index]
    cta_label = html.escape(study["cta_label"])
    cta_href = html.escape(study["cta_href"], quote=True)

    sections_html = "\n".join(
        f"""          <section class="case-section" aria-labelledby="{key}-title">
            <h2 class="case-section-title" id="{key}-title">{title}</h2>
            <p>{html.escape(study[key])}</p>
          </section>"""
        for key, title in SECTIONS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{page_title}</title>
    <meta name="description" content="{meta}" />
    <link rel="canonical" href="{canonical}" />
{head_metadata}
    <link rel="icon" href="/assets/logo.png" type="image/png" />
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
      <a class="top-link" href="/insights">Insights</a>
    </header>

    <main>
      <article class="block case-study" data-slug="{slug}" data-engagement="{html.escape(engagement)}">
        <p class="case-eyebrow">{org}</p>
        <h1 class="page-title case-title">{headline}</h1>
        <p class="case-disclaimer">{disclaimer}</p>
{sections_html}
        <p class="case-cta-row">
          <a class="cta" href="{cta_href}">{cta_label}</a>
          <a class="cta cta-secondary" href="/#proof">All proof</a>
        </p>
      </article>
    </main>

    <footer class="foot">
      <p>saberistic · software development</p>
    </footer>
  </body>
</html>
"""
