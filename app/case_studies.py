"""Case study data and HTML rendering for /work/{slug} pages."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal

from app.metadata import OG_IMAGE, OG_IMAGE_ALT
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

PROOF_META_LABELS: dict[Engagement, str] = {
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


def _proof_meta(study: dict[str, Any]) -> str:
    engagement = study["engagement"]
    label = PROOF_META_LABELS[engagement]  # type: ignore[index]
    org = "Saberistic" if engagement == "saberistic" else study["org"]
    return f"{org} · {label}"


def render_case_studies_index(path: Path | None = None) -> str:
    """Render the /case-studies listing page."""
    studies = load_case_studies(path)
    title = "Case studies — saberistic"
    description = (
        "Outcome-oriented architecture case studies — employer roles, founder "
        "ventures, and Saberistic engagements."
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": title,
        "description": description,
        "url": f"{CANONICAL_BASE}/case-studies",
        "isPartOf": {
            "@type": "ProfessionalService",
            "name": "saberistic",
            "url": f"{CANONICAL_BASE}/",
        },
    }
    title_esc = html.escape(title)
    desc_esc = html.escape(description)
    canonical = f"{CANONICAL_BASE}/case-studies"
    canonical_esc = html.escape(canonical, quote=True)
    ld_json = json.dumps(json_ld, ensure_ascii=False)

    items = "\n".join(
        f"""          <li class="proof-item">
            <a class="proof-link" href="/work/{html.escape(study['slug'])}">
              <span class="proof-headline">{html.escape(study['headline'])}</span>
              <span class="proof-meta">{html.escape(_proof_meta(study))}</span>
            </a>
            <p class="proof-summary">{html.escape(study['problem'])}</p>
          </li>"""
        for study in studies
    )

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title_esc}</title>
    <meta name="description" content="{desc_esc}" />
    <link rel="canonical" href="{canonical_esc}" />
    <meta property="og:type" content="website" />
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
      <a class="top-link" href="/insights">Insights</a>
    </header>

    <main>
      <section class="block" aria-labelledby="case-studies-title">
        <h1 class="page-title" id="case-studies-title">Case studies</h1>
        <p class="proof-lede">
          Outcome-oriented case studies — problems addressed, interventions
          applied, and results delivered. Employer roles are distinguished from
          Saberistic engagements.
        </p>
        <ul class="proof-list">
{items}
        </ul>
        <p class="case-studies-cta">
          Facing a similar architecture, reliability, security, or
          technical-leadership problem?
        </p>
        <p class="cta-row">
          <a class="cta" href="/brief">Request an Architecture Diagnostic</a>
        </p>
      </section>
    </main>

    <footer class="foot">
      <p>saberistic · technical architecture &amp; engineering leadership</p>
    </footer>
  </body>
</html>
"""


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
