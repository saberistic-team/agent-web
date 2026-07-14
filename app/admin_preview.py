"""Mock admin dashboard data for ADMIN_PREVIEW_MODE screenshots.

Never used in production — only when ``Settings.admin_preview_enabled`` is true.
"""

from __future__ import annotations

import html
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


COMPANY_NAMES = (
    "Northwind Labs",
    "Helios Rail",
    "Cedar Protocol",
    "Aperture Freight",
    "Meridian Stack",
    "Volt Spiral",
    "Oakline Systems",
    "Bright Harbor",
    "Kite Ledger",
    "Summit Relay",
)

CONTACT_FIRST = (
    "Alex",
    "Sam",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Quinn",
    "Avery",
)

CONTACT_LAST = (
    "Nguyen",
    "Patel",
    "Okoro",
    "Berg",
    "Silva",
    "Khan",
    "Park",
    "Ellis",
)

STATUSES = ("new", "paid", "follow-up", "closed")
SOURCES = ("brief", "referral", "inbound", "partner")


@dataclass(frozen=True)
class PreviewBriefRow:
    company: str
    contact: str
    email: str
    status: str
    source: str
    submitted_at: str
    amount_cents: int


@dataclass(frozen=True)
class PreviewDashboardData:
    """Fake intake/CRM snapshot for screenshot preview."""

    briefs_this_week: int
    paid_this_week: int
    open_prospects: int
    sessions_active: int
    recent_briefs: tuple[PreviewBriefRow, ...]
    preview_banner: str
    generated_at: str


def _preview_rng() -> random.Random:
    """Randomize per process; optional ``ADMIN_PREVIEW_SEED`` for stable tests."""
    raw = (os.environ.get("ADMIN_PREVIEW_SEED") or "").strip()
    if raw:
        try:
            return random.Random(int(raw))
        except ValueError:
            return random.Random(raw)
    return random.Random()


def _slug_email(first: str, last: str, company: str, rng: random.Random) -> str:
    domain = company.lower().replace(" ", "") + rng.choice(
        (".io", ".com", ".co", ".dev")
    )
    return f"{first.lower()}.{last.lower()}@{domain}"


def build_preview_dashboard_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> PreviewDashboardData:
    """Build a randomized but plausible admin dashboard payload."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)

    briefs_this_week = rng.randint(4, 28)
    paid_this_week = rng.randint(1, max(1, briefs_this_week // 2))
    open_prospects = rng.randint(6, 40)
    sessions_active = rng.randint(1, 4)

    row_count = rng.randint(4, 8)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    rows: list[PreviewBriefRow] = []
    for i in range(row_count):
        company = companies[i % len(companies)]
        first = rng.choice(CONTACT_FIRST)
        last = rng.choice(CONTACT_LAST)
        hours_ago = rng.randint(1, 96)
        submitted = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 50))
        rows.append(
            PreviewBriefRow(
                company=company,
                contact=f"{first} {last}",
                email=_slug_email(first, last, company, rng),
                status=rng.choice(STATUSES),
                source=rng.choice(SOURCES),
                submitted_at=submitted.strftime("%Y-%m-%d %H:%M UTC"),
                amount_cents=rng.choice((20_000, 20_000, 35_000, 50_000)),
            )
        )

    return PreviewDashboardData(
        briefs_this_week=briefs_this_week,
        paid_this_week=paid_this_week,
        open_prospects=open_prospects,
        sessions_active=sessions_active,
        recent_briefs=tuple(rows),
        preview_banner="Preview data — not production",
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def _format_amount(cents: int) -> str:
    return f"${cents / 100:.0f}"


def render_preview_dashboard_main(data: PreviewDashboardData) -> str:
    """HTML main fragment for the admin shell dashboard in preview mode."""
    rows = "\n".join(
        (
            "<tr>"
            f"<td>{html.escape(row.company)}</td>"
            f"<td>{html.escape(row.contact)}</td>"
            f"<td>{html.escape(row.email)}</td>"
            f"<td><span class=\"admin-status admin-status-{html.escape(row.status)}\">"
            f"{html.escape(row.status)}</span></td>"
            f"<td>{html.escape(row.source)}</td>"
            f"<td>{html.escape(_format_amount(row.amount_cents))}</td>"
            f"<td>{html.escape(row.submitted_at)}</td>"
            "</tr>"
        )
        for row in data.recent_briefs
    )
    return f"""        <section class="admin-empty" aria-labelledby="admin-home-title">
          <p class="admin-preview-banner" role="status">{html.escape(data.preview_banner)}</p>
          <p class="admin-eyebrow">Preview</p>
          <h1 class="admin-title" id="admin-home-title">Dashboard</h1>
          <p class="admin-lede">
            Mock intake snapshot for screenshots
            (<time datetime="{html.escape(data.generated_at)}">{html.escape(data.generated_at)}</time>).
          </p>
          <dl class="admin-stat-row">
            <div>
              <dt>Briefs (7d)</dt>
              <dd>{data.briefs_this_week}</dd>
            </div>
            <div>
              <dt>Paid (7d)</dt>
              <dd>{data.paid_this_week}</dd>
            </div>
            <div>
              <dt>Open prospects</dt>
              <dd>{data.open_prospects}</dd>
            </div>
            <div>
              <dt>Active sessions</dt>
              <dd>{data.sessions_active}</dd>
            </div>
          </dl>
          <h2 class="admin-section-title">Recent briefs</h2>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col">Contact</th>
                  <th scope="col">Email</th>
                  <th scope="col">Status</th>
                  <th scope="col">Source</th>
                  <th scope="col">Amount</th>
                  <th scope="col">Submitted</th>
                </tr>
              </thead>
              <tbody>
                {rows}
              </tbody>
            </table>
          </div>
        </section>"""

