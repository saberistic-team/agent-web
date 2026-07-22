"""Admin HTML for LinkedIn-derived relationship metrics on contact pages."""

from __future__ import annotations

import html
import json
from typing import Any

from app.contacts import CRM_CONTEXT_TAGS, RELATIONSHIP_STRENGTHS, format_crm_context_tags


def _esc(value: Any) -> str:
    return "" if value is None else html.escape(str(value), quote=True)


def _metric_row(label: str, value: Any) -> str:
    display = _esc(value) if value not in (None, "") else "—"
    return f'<div><dt>{_esc(label)}</dt><dd>{display}</dd></div>'


def render_computed_relationship_metrics(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return """<section class="contact-metrics-section" aria-labelledby="contact-metrics-title">
      <h2 class="admin-section-heading" id="contact-metrics-title">LinkedIn-derived metrics</h2>
      <p class="admin-note">No computed metrics yet — import a LinkedIn export with message metadata.</p>
    </section>"""

    scoring = metrics.get("scoring_inputs") or metrics
    scoring_json = html.escape(json.dumps(scoring, sort_keys=True, indent=2))
    return f"""<section class="contact-metrics-section" aria-labelledby="contact-metrics-title">
      <h2 class="admin-section-heading" id="contact-metrics-title">LinkedIn-derived metrics</h2>
      <p class="admin-note" role="note">Computed from export metadata only — not operator judgment.</p>
      <dl class="research-provenance contact-metrics-dl">
        {_metric_row("Connection date", metrics.get("connection_date"))}
        {_metric_row("Conversations", metrics.get("conversation_count"))}
        {_metric_row("Messages (deduplicated)", metrics.get("message_count"))}
        {_metric_row("Inbound (they wrote you)", metrics.get("inbound_count"))}
        {_metric_row("Outbound (you wrote them)", metrics.get("outbound_count"))}
        {_metric_row("First interaction", metrics.get("first_interaction_at"))}
        {_metric_row("Last interaction", metrics.get("last_interaction_at"))}
        {_metric_row("Active in last 30 days", "Yes" if metrics.get("recent_interaction_30d") else "No")}
        {_metric_row("Active in last 90 days", "Yes" if metrics.get("recent_interaction_90d") else "No")}
        {_metric_row("Two-way conversation", "Yes" if metrics.get("two_way_conversation") else "No")}
      </dl>
      <details class="contact-metrics-inputs">
        <summary>Scoring inputs (deterministic)</summary>
        <pre class="contact-metrics-pre">{scoring_json}</pre>
      </details>
    </section>"""


def render_operator_judgment_fields(
    contact: dict[str, Any],
    *,
    crm_context_checkboxes: str,
) -> str:
    relationship = RELATIONSHIP_STRENGTHS.get(
        str(contact.get("relationship_strength")),
        contact.get("relationship_strength"),
    )
    return f"""<section class="contact-judgment-section" aria-labelledby="contact-judgment-title">
      <h2 class="admin-section-heading" id="contact-judgment-title">Operator judgment</h2>
      <p class="admin-note" role="note">Human-assigned context — separate from computed LinkedIn metrics.</p>
      <dl class="research-provenance contact-judgment-dl">
        {_metric_row("Relationship strength", relationship)}
        {_metric_row("CRM context", format_crm_context_tags(contact.get("crm_context_tags")))}
        {_metric_row("Last interaction (CRM field)", contact.get("last_interaction_at"))}
        {_metric_row("Notes", contact.get("notes"))}
      </dl>
      {crm_context_checkboxes}
    </section>"""


def crm_context_checkbox_field(selected: list[str] | None) -> str:
    selected_set = set(selected or [])
    boxes = "\n".join(
        f'<label class="admin-checkbox"><input type="checkbox" name="crm_context_tags" value="{_esc(key)}"'
        f'{" checked" if key in selected_set else ""} /> {_esc(label)}</label>'
        for key, label in CRM_CONTEXT_TAGS.items()
    )
    return f"""<fieldset class="field contact-crm-context">
      <legend>CRM context</legend>
      <p class="admin-note">Explicit relationship context for scoring — not inferred from messages.</p>
      {boxes}
    </fieldset>"""
