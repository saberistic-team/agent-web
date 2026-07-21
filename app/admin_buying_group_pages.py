"""Admin HTML for buying-group coverage and warm-introduction paths."""

from __future__ import annotations

import html
from typing import Any

from app.buying_group import (
    COVERAGE_STATUS_LABELS,
    WarmIntroPath,
    build_buying_group_view,
    safe_profile_link,
)
from app.contacts import BUYING_ROLES


def _render_coverage_badge(status: str) -> str:
    label = COVERAGE_STATUS_LABELS.get(status, status)  # type: ignore[arg-type]
    return (
        f'<span class="coverage-badge coverage-badge--{html.escape(status, quote=True)}" '
        f'role="status">{html.escape(label)}</span>'
    )


def _render_contact_entry(entry: Any) -> str:
    contact_id = html.escape(str(entry.contact_id), quote=True)
    name = html.escape(str(entry.display_name))
    title = html.escape(str(entry.title or ""))
    title_html = f' <span class="admin-meta">({title})</span>' if title else ""
    profile_html = ""
    if entry.profile_url:
        link = safe_profile_link(str(entry.profile_url), label="Profile")
        if link:
            profile_html = f' <span class="buying-profile">{link}</span>'
    note_html = ""
    if entry.status_note:
        note_html = (
            f'<p class="coverage-note">{html.escape(str(entry.status_note))}</p>'
        )
    also_html = ""
    if entry.also_roles:
        labels = ", ".join(
            BUYING_ROLES.get(role, role) for role in entry.also_roles
        )
        also_html = (
            f'<p class="coverage-also-roles">Also covers: {html.escape(labels)}</p>'
        )
    return f"""
            <li class="coverage-entry coverage-entry--{html.escape(str(entry.status), quote=True)}">
              <div class="coverage-entry-header">
                <a href="/admin/contacts/{contact_id}">{name}</a>{title_html}
                {_render_coverage_badge(str(entry.status))}
              </div>
              {profile_html}
              {also_html}
              {note_html}
            </li>"""


def _render_slot(slot: Any) -> str:
    if slot.entries:
        entries_html = "".join(_render_contact_entry(entry) for entry in slot.entries)
        body = f'<ul class="coverage-entry-list">{entries_html}\n            </ul>'
    else:
        body = (
            '<p class="coverage-gap">No contact mapped to this role yet. '
            "Record a research gap instead of inventing a placeholder contact.</p>"
        )
    return f"""
          <section class="buying-group-slot buying-group-slot--{html.escape(str(slot.slot_status), quote=True)}" aria-labelledby="slot-{html.escape(str(slot.role_key), quote=True)}">
            <div class="buying-group-slot-header">
              <h3 class="admin-section-heading" id="slot-{html.escape(str(slot.role_key), quote=True)}">{html.escape(str(slot.role_label))}</h3>
              {_render_coverage_badge(str(slot.slot_status))}
            </div>
            {body}
          </section>"""


def _render_warm_intro_path(path: WarmIntroPath) -> str:
    introducer_id = html.escape(str(path.introducer_id), quote=True)
    name = html.escape(str(path.introducer_name))
    profile_html = ""
    if path.profile_url:
        link = safe_profile_link(str(path.profile_url), label="Profile")
        if link:
            profile_html = f'<p class="warm-intro-profile">{link}</p>'
    context = html.escape(str(path.relationship_context))
    metrics = html.escape(str(path.interaction_metrics))
    return f"""
          <article class="warm-intro-path">
            <header class="warm-intro-path-header">
              <h3 class="admin-section-heading"><a href="/admin/contacts/{introducer_id}">{name}</a></h3>
            </header>
            {profile_html}
            <dl class="warm-intro-metrics">
              <div><dt>Relationship context</dt><dd>{context}</dd></div>
              <div><dt>Interaction metrics</dt><dd>{metrics}</dd></div>
            </dl>
          </article>"""


def render_buying_group_section(
    contacts: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> str:
    view = build_buying_group_view(contacts, records)
    slots_html = "".join(_render_slot(slot) for slot in view.slots)
    warm_html = render_warm_intro_section(view.warm_intro_paths)
    return f"""
          <h2 class="admin-section-heading">Buying-group coverage</h2>
          <p class="admin-lede">Coverage for founder, CTO, VP Engineering, investor, and introducer roles.</p>
          <div class="buying-group-grid">{slots_html}
          </div>
          {warm_html}"""


def render_warm_intro_section(paths: tuple[WarmIntroPath, ...]) -> str:
    if not paths:
        return """
          <h2 class="admin-section-heading">Warm introduction paths</h2>
          <p class="admin-note">No introducer contacts with relationship context yet.</p>"""
    paths_html = "".join(_render_warm_intro_path(path) for path in paths)
    return f"""
          <h2 class="admin-section-heading">Warm introduction paths</h2>
          <p class="admin-lede">Former-colleague and relationship context with derived interaction metrics.</p>
          <div class="warm-intro-list">{paths_html}
          </div>"""
