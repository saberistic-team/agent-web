"""Case study data and HTML rendering for /work/{slug} and /case-studies pages."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal

from app.insights import _render_head, _render_page_shell
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

INDEX_META_LABELS: dict[Engagement, str] = {
    "employer": "prior employer role",
    "founder": "founder venture",
    "saberistic": "sanitized diagnostic",
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


def render_case_studies_index(path: Path | None = None) -> str:
    """Render the /case-studies listing page."""
    studies = load_case_studies(path)
    title = "Case studies — saberistic"
    description = (
        "Five outcome-oriented case studies — infrastructure, security, "
        "engineering leadership, and architecture diagnostics."
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "description": description,
        "url": f"{CANONICAL_BASE}/case-studies",
        "isPartOf": {"@type": "WebSite", "name": "saberistic", "url": f"{CANONICAL_BASE}/"},
    }

    head = _render_head(
        title=title,
        description=description,
        canonical_path="/case-studies",
        og_type="website",
        json_ld=json_ld,
        feed_link=True,
    )

    items: list[str] = []
    for study in studies:
        engagement = study["engagement"]
        meta_label = INDEX_META_LABELS[engagement]  # type: ignore[index]
        org_display = "Saberistic" if engagement == "saberistic" else study["org"]
        org = html.escape(org_display)
        slug = html.escape(study["slug"])
        headline = html.escape(study["headline"])
        summary = html.escape(study["result"])
        items.append(
            f"""          <li class="proof-item">
            <a class="proof-link" href="/work/{slug}">
              <span class="proof-headline">{headline}</span>
              <span class="proof-meta">{org} · {html.escape(meta_label)}</span>
            </a>
            <p class="proof-summary">{summary}</p>
          </li>"""
        )

    items_html = "\n".join(items)

    main = f"""      <section class="block case-studies-index" aria-labelledby="case-studies-title">
        <h1 class="page-title" id="case-studies-title">Case studies</h1>
        <p class="proof-lede">
          Outcome-oriented case studies — problems addressed, interventions
          applied, and results delivered. Employer roles are distinguished from
          Saberistic engagements.
        </p>
        <ul class="proof-list">
{items_html}
        </ul>
        <p class="case-cta-row">
          Facing a similar architecture, reliability, security, or
          technical-leadership problem?
          <a class="cta" href="/brief">Request an Architecture Diagnostic</a>
        </p>
      </section>"""

    return _render_page_shell(head=head, main=main, top_link="Insights")


def render_case_study_page(study: dict[str, Any]) -> str:
    """Render a full HTML page for one case study."""
    slug = html.escape(study["slug"])
    org = html.escape(study["org"])
    headline = html.escape(study["headline"])
    meta = html.escape(study["meta_description"])
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
    <title>{org} — {headline} · saberistic</title>
    <meta name="description" content="{meta}" />
    <link rel="canonical" href="https://saberistic.com/work/{slug}" />
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
