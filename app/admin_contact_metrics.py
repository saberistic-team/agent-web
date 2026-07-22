"""Admin HTML for computed LinkedIn relationship metrics."""

from __future__ import annotations

import html
import json
from typing import Any

from app.linkedin_relationship_metrics import finalize_stored_metrics, parse_stored_metrics


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _format_metric_value(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if value is None or value == "":
        return "—"
    return str(value)


def render_computed_linkedin_metrics_panel(
    contact: dict[str, Any],
    *,
    reference_metrics: dict[str, Any] | None = None,
) -> str:
    """Render LinkedIn-derived metrics separately from operator judgment fields."""
    raw_metrics = reference_metrics if reference_metrics is not None else contact.get("linkedin_metrics")
    metrics = finalize_stored_metrics(
        parse_stored_metrics(raw_metrics),
        former_colleague=bool(contact.get("former_colleague")),
        warm_introducer=bool(contact.get("warm_introducer")),
    )
    if not metrics or not metrics.get("schema_version"):
        return """<section class="admin-section linkedin-metrics-panel" aria-labelledby="linkedin-metrics-title">
      <p class="admin-eyebrow">LinkedIn export</p>
      <h2 class="admin-section-heading" id="linkedin-metrics-title">Computed relationship metrics</h2>
      <p class="admin-note">No LinkedIn message metadata has been imported for this contact yet.</p>
    </section>"""

    score_inputs = metrics.get("score_inputs") or {}
    rows = (
        ("Connection date", metrics.get("connection_date")),
        ("Conversations", metrics.get("conversation_count")),
        ("Inbound messages", metrics.get("inbound_count")),
        ("Outbound messages", metrics.get("outbound_count")),
        ("Two-way conversation", metrics.get("two_way")),
        ("First interaction", metrics.get("first_interaction_at")),
        ("Last interaction", metrics.get("last_interaction_at")),
        ("Active in last 30 days", metrics.get("recent_30d")),
        ("Active in last 90 days", metrics.get("recent_90d")),
        ("Active in last 180 days", metrics.get("recent_180d")),
        ("Computed score", metrics.get("computed_score")),
    )
    metric_rows = "".join(
        f"<div><dt>{_esc(label)}</dt><dd>{_esc(_format_metric_value(value))}</dd></div>"
        for label, value in rows
    )
    inputs_json = json.dumps(score_inputs, sort_keys=True, indent=2)
    return f"""<section class="admin-section linkedin-metrics-panel" aria-labelledby="linkedin-metrics-title">
      <p class="admin-eyebrow">LinkedIn export</p>
      <h2 class="admin-section-heading" id="linkedin-metrics-title">Computed relationship metrics</h2>
      <p class="admin-note" role="note">
        Derived from LinkedIn connection and message metadata only. Message bodies are never stored.
        These values are deterministic and separate from operator judgment below.
      </p>
      <dl class="research-provenance linkedin-metrics-dl">{metric_rows}</dl>
      <details class="linkedin-metrics-inputs">
        <summary>Scoring inputs (deterministic)</summary>
        <pre class="linkedin-metrics-pre">{_esc(inputs_json)}</pre>
      </details>
    </section>"""


def render_human_judgment_panel(contact: dict[str, Any]) -> str:
    from app.contacts import RELATIONSHIP_STRENGTHS, format_buying_roles

    relationship = RELATIONSHIP_STRENGTHS.get(
        str(contact.get("relationship_strength")), contact.get("relationship_strength")
    )
    rows = (
        ("Relationship strength", relationship),
        ("Last interaction (manual)", contact.get("last_interaction_at")),
        ("Former colleague", "Yes" if contact.get("former_colleague") else "No"),
        ("Warm introducer", "Yes" if contact.get("warm_introducer") else "No"),
        ("Buying roles", format_buying_roles(contact.get("buying_roles"))),
        ("Notes", contact.get("notes")),
    )
    fact_rows = "".join(
        f"<div><dt>{_esc(label)}</dt><dd>{_esc(_format_metric_value(value))}</dd></div>"
        for label, value in rows
    )
    return f"""<section class="admin-section human-judgment-panel" aria-labelledby="human-judgment-title">
      <p class="admin-eyebrow">Operator judgment</p>
      <h2 class="admin-section-heading" id="human-judgment-title">CRM context</h2>
      <p class="admin-note" role="note">Human-entered fields used for outreach decisions. Not inferred from private messages.</p>
      <dl class="research-provenance human-judgment-dl">{fact_rows}</dl>
    </section>"""
