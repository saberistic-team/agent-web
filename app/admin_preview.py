"""Mock admin page data for ADMIN_PREVIEW_MODE screenshots.

Never used in production — only when ``Settings.admin_preview_enabled`` is true.
"""

from __future__ import annotations

import html
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.acquisition_dashboard import (
    AcquisitionDashboardData,
    CompanyAttentionRow,
    CountBucket,
    EvidenceRow,
    NextActionRow,
)
from app.pipeline_stages import PIPELINE_STAGES
from app.companies import COMPANY_CATEGORIES, COMPANY_STAGES


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
SIGNAL_TYPES = ("hiring", "funding", "tech-stack", "intent", "news")
IMPORT_STATUSES = ("queued", "running", "complete", "failed")
CONTENT_KINDS = ("insight", "case-study", "landing", "brief copy")
BRIEF_PAYMENT_STATUSES = ("pending_payment", "paid", "abandoned")
# Reserved preview detail id for ADMIN_PREVIEW_MODE database-unavailable screenshots.
PREVIEW_BRIEF_DATABASE_ERROR_ID = 503
# Brief already linked to CRM/pipeline in preview screenshots.
PREVIEW_BRIEF_CONVERTED_ID = 3
# Brief convert preview with explicit domain/email matches for Reviewer shots.
PREVIEW_BRIEF_CONVERT_MATCHES_ID = 4
PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR = (
    "Select an existing company match or choose to create a new company."
)
# Archived contact id for restore-conflict screenshots in ADMIN_PREVIEW_MODE.
PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID = UUID(
    "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
)
PREVIEW_CONTACT_RESTORE_CONFLICT_ACTIVE_ID = UUID(
    "ffffffff-ffff-ffff-ffff-ffffffffffff"
)
# Company/contact detail pages for Archive and Restore screenshot states.
PREVIEW_COMPANY_DETAIL_ARCHIVE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd01")
PREVIEW_COMPANY_DETAIL_RESTORE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd02")
PREVIEW_CONTACT_DETAIL_ARCHIVE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd03")
PREVIEW_CONTACT_DETAIL_RESTORE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd04")
BRIEF_TEXTS = (
    "Need a technical architecture review of our payments platform — "
    "API boundaries, retention, and rollout sequencing.",
    "Evaluating whether to rebuild the billing monolith or carve services. "
    "Want an outside view before Series B.",
    "Multi-tenant CRM sync is leaking PII across accounts. Diagnose root cause "
    "and propose a containment plan.",
    "Edge deploy latency spiked after the last k8s migration. Looking for a "
    "concrete diagnosis and go/no-go on rollback.",
)
UTM_SOURCES = ("linkedin", "referral", "google", "newsletter", "partner")
UTM_MEDIUMS = ("social", "cpc", "email", "organic", None)
UTM_CAMPAIGNS = ("spring-launch", "architecture-diagnostic", "inbound-q3", None)

PREVIEW_PIPELINE_COMPANY_IDS = (
    UUID("11111111-1111-1111-1111-111111111111"),
    UUID("22222222-2222-2222-2222-222222222222"),
    UUID("33333333-3333-3333-3333-333333333333"),
    UUID("44444444-4444-4444-4444-444444444444"),
    UUID("55555555-5555-5555-5555-555555555555"),
)
_SECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "/admin/companies": ("Company", "Category", "Stage", "Target", "Verified"),
    "/admin/contacts": ("Name", "Roles", "Company", "Email", "Last touch"),
    "/admin/signals": ("Signal", "Company", "Score", "Source", "Seen"),
    "/admin/pipeline": ("Deal", "Company", "Stage", "Value", "Next step"),
    "/admin/imports": ("Job", "Rows", "Status", "Source", "Started"),
    "/admin/discovery": ("List", "Prospects", "Filter", "Owner", "Refreshed"),
    "/admin/analytics": ("Metric", "Period", "Value", "Delta", "Segment"),
    "/admin/content": ("Title", "Kind", "Status", "Author", "Updated"),
    "/admin/settings": ("Setting", "Value", "Scope", "Owner", "Changed"),
}


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


def _preview_uuid(rng: random.Random) -> str:
    return str(UUID(int=rng.getrandbits(128), version=4))


def build_preview_acquisition_dashboard_data(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> AcquisitionDashboardData:
    """Randomized acquisition dashboard for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)

    def _buckets(registry: dict[str, str]) -> tuple[CountBucket, ...]:
        chosen = rng.sample(list(registry.keys()), k=min(len(registry), rng.randint(3, 5)))
        return tuple(
            CountBucket(key=key, label=registry[key], count=rng.randint(1, 18))
            for key in chosen
        )

    def _next_actions(*, overdue: bool) -> tuple[NextActionRow, ...]:
        stage_keys = list(PIPELINE_STAGES.keys())
        rows: list[NextActionRow] = []
        for i in range(rng.randint(3, 6)):
            company = companies[i % len(companies)]
            delta_days = rng.randint(1, 10)
            due_at = now - timedelta(days=delta_days) if overdue else now + timedelta(days=delta_days)
            rows.append(
                NextActionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    pipeline_stage=stage_keys[i % len(stage_keys)],
                    pipeline_owner=rng.choice(("alex", "sam", "preview")),
                    next_action=rng.choice(BRIEF_TEXTS)[:140],
                    next_action_due_at=due_at,
                )
            )
        return tuple(rows)

    def _evidence(*, stale: bool) -> tuple[EvidenceRow, ...]:
        rows: list[EvidenceRow] = []
        for i in range(rng.randint(3, 5)):
            company = companies[(i + 2) % len(companies)]
            created = now - timedelta(days=rng.randint(1, 20))
            expires = now - timedelta(days=rng.randint(1, 5)) if stale else now + timedelta(days=30)
            rows.append(
                EvidenceRow(
                    record_id=_preview_uuid(rng),
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    record_type=rng.choice(("verified_fact", "public_signal")),
                    body=rng.choice(BRIEF_TEXTS)[:120],
                    created_at=created,
                    expires_at=expires,
                )
            )
        return tuple(rows)

    def _without_decision_maker() -> tuple[CompanyAttentionRow, ...]:
        """Uncovered targets for screenshots — lack qualifying active decision-makers."""
        stage_keys = tuple(COMPANY_STAGES.keys())
        category_keys = tuple(COMPANY_CATEGORIES.keys())
        uncovered_names = ("Meridian Stack", "Volt Spiral", "Aperture Freight")
        rows: list[CompanyAttentionRow] = []
        for i, company in enumerate(uncovered_names):
            rows.append(
                CompanyAttentionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    target_status="target" if i == 0 else "watching",
                    category=category_keys[i % len(category_keys)],
                    stage=stage_keys[i % len(stage_keys)],
                )
            )
        return tuple(rows)

    def _attention(*, pipeline_only: bool = False) -> tuple[CompanyAttentionRow, ...]:
        stage_keys = tuple(COMPANY_STAGES.keys())
        category_keys = tuple(COMPANY_CATEGORIES.keys())
        pipeline_keys = tuple(PIPELINE_STAGES.keys())
        rows: list[CompanyAttentionRow] = []
        for i in range(rng.randint(2, 4)):
            company = companies[(i + 4) % len(companies)]
            stage = rng.choice(stage_keys)
            category = rng.choice(category_keys)
            rows.append(
                CompanyAttentionRow(
                    company_id=_preview_uuid(rng),
                    company_name=company,
                    target_status=rng.choice(("target", "watching")),
                    category=category,
                    stage=stage,
                    pipeline_stage=rng.choice(pipeline_keys) if pipeline_only else None,
                )
            )
        return tuple(rows)

    return AcquisitionDashboardData(
        company_counts_by_stage=_buckets(COMPANY_STAGES),
        company_counts_by_category=_buckets(COMPANY_CATEGORIES),
        contact_counts_by_stage=_buckets(COMPANY_STAGES),
        contact_counts_by_category=_buckets(COMPANY_CATEGORIES),
        overdue_actions=_next_actions(overdue=True),
        upcoming_actions=_next_actions(overdue=False),
        recent_evidence=_evidence(stale=False),
        stale_evidence=_evidence(stale=True),
        without_decision_maker=_without_decision_maker(),
        without_next_action=_attention(pipeline_only=True),
        generated_at=now,
    )


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
          <h2 class="admin-section-title">Recent submissions</h2>
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


def _person(rng: random.Random) -> str:
    return f"{rng.choice(CONTACT_FIRST)} {rng.choice(CONTACT_LAST)}"


def _relative_stamp(rng: random.Random, now: datetime) -> str:
    hours_ago = rng.randint(1, 120)
    stamp = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 50))
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def build_preview_section_rows(
    active_path: str,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Build randomized table rows for an admin section preview page."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    count = rng.randint(4, 8)
    rows: list[tuple[str, ...]] = []

    for i in range(count):
        company = companies[i % len(companies)]
        person = _person(rng)
        first, last = person.split(" ", 1)
        stamp = _relative_stamp(rng, now)
        if active_path == "/admin/companies":
            rows.append(
                (
                    company,
                    rng.choice(("Fintech", "AI infrastructure", "Digital assets", "Investor", "Other")),
                    rng.choice(("Pre-seed", "Seed", "Series A", "Series B+")),
                    rng.choice(("Target", "Watching", "Not a fit")),
                    stamp,
                )
            )
        elif active_path == "/admin/contacts":
            from app.contacts import BUYING_ROLES

            role_count = rng.randint(1, 3)
            roles = rng.sample(list(BUYING_ROLES.values()), k=min(role_count, len(BUYING_ROLES)))
            rows.append(
                (
                    person,
                    ", ".join(roles),
                    company,
                    _slug_email(first, last, company, rng),
                    stamp,
                )
            )
        elif active_path == "/admin/signals":
            rows.append(
                (
                    rng.choice(SIGNAL_TYPES),
                    company,
                    str(rng.randint(42, 98)),
                    rng.choice(SOURCES),
                    stamp,
                )
            )
        elif active_path == "/admin/pipeline":
            stage_key = rng.choice(list(PIPELINE_STAGES))
            rows.append(
                (
                    f"{company.split()[0]} pilot",
                    company,
                    PIPELINE_STAGES[stage_key],
                    _format_amount(rng.choice((20_000, 35_000, 50_000, 75_000))),
                    stamp,
                )
            )
        elif active_path == "/admin/imports":
            rows.append(
                (
                    f"import-{rng.randint(100, 999)}",
                    str(rng.randint(40, 2400)),
                    rng.choice(IMPORT_STATUSES),
                    rng.choice(("csv", "enrichment", "crm sync")),
                    stamp,
                )
            )
        elif active_path == "/admin/discovery":
            rows.append(
                (
                    f"{company.split()[0]} ICP",
                    str(rng.randint(18, 220)),
                    rng.choice(("geo+size", "tech stack", "hiring")),
                    person,
                    stamp,
                )
            )
        elif active_path == "/admin/analytics":
            rows.append(
                (
                    rng.choice(("Conversion", "Paid rate", "Time-to-reply", "Pipeline $")),
                    rng.choice(("7d", "30d", "90d")),
                    str(rng.randint(8, 96)),
                    f"{rng.choice(('+', '-'))}{rng.randint(1, 18)}%",
                    rng.choice(("all", "inbound", "partner")),
                )
            )
        elif active_path == "/admin/content":
            rows.append(
                (
                    f"{company} note",
                    rng.choice(CONTENT_KINDS),
                    rng.choice(("draft", "review", "published")),
                    person,
                    stamp,
                )
            )
        elif active_path == "/admin/settings":
            rows.append(
                (
                    rng.choice(
                        ("session TTL", "MFA required", "webhook URL", "timezone")
                    ),
                    rng.choice(("14d", "on", "https://hooks.example/…", "UTC")),
                    rng.choice(("security", "integrations", "team")),
                    person,
                    stamp,
                )
            )
        else:
            rows.append((company, person, stamp, rng.choice(STATUSES), "preview"))

    return tuple(rows)


def build_preview_pipeline_companies(
    *,
    stage_filter: str | None = None,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized pipeline companies for ADMIN_PREVIEW_MODE."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    stage_keys = list(PIPELINE_STAGES)
    companies: list[dict[str, object]] = []
    for index, company_id in enumerate(PREVIEW_PIPELINE_COMPANY_IDS):
        stage_key = stage_keys[index % len(stage_keys)]
        if stage_filter and stage_key != stage_filter:
            continue
        due_offset = rng.randint(-5, 12)
        companies.append(
            {
                "id": company_id,
                "name": COMPANY_NAMES[index % len(COMPANY_NAMES)],
                "pipeline_stage": stage_key,
                "expected_value_cents": rng.choice((25_000, 50_000, 75_000, 120_000)),
                "next_action": rng.choice(
                    (
                        "Send diagnostic proposal",
                        "Schedule discovery call",
                        "Follow up on reply",
                        None,
                    )
                ),
                "next_action_due_at": now + timedelta(days=due_offset),
                "pipeline_owner": rng.choice(("alex", "sam", "preview")),
            }
        )
    return companies


def build_preview_pipeline_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]] | None:
    """Preview pipeline detail for a fixed company id."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = build_preview_pipeline_companies(rng=rng, now=now)
    company = next((row for row in companies if row["id"] == company_id), None)
    if company is None:
        return None
    stage = str(company["pipeline_stage"])
    history = [
        {
            "changed_at": now - timedelta(days=14),
            "from_stage": "researching",
            "to_stage": "qualified",
            "changed_by": "preview",
        },
        {
            "changed_at": now - timedelta(days=7),
            "from_stage": "qualified",
            "to_stage": stage,
            "changed_by": "preview",
        },
    ]
    activities = [
        {
            "created_at": now - timedelta(days=3),
            "activity_type": "outreach",
            "summary": "Sent intro email with diagnostic overview.",
        },
        {
            "created_at": now - timedelta(days=1),
            "activity_type": "reply",
            "summary": "Prospect asked for pricing and timeline.",
        },
    ]
    if company_id == PREVIEW_PIPELINE_COMPANY_IDS[1]:
        company = {
            **company,
            "next_action": None,
            "next_action_due_at": None,
            "pipeline_owner": None,
            "expected_value_cents": None,
        }
    return company, history, activities


def _brief_website(company: str, rng: random.Random) -> str:
    slug = company.lower().replace(" ", "-")
    tld = rng.choice((".io", ".com", ".co", ".dev"))
    path = rng.choice(("", "/platform", "/engineering", "/products"))
    return f"https://{slug}{tld}{path}"


def _brief_email(company: str, rng: random.Random) -> str:
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    return _slug_email(first, last, company, rng)


def build_preview_brief_rows(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized project-brief list rows for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    companies = list(COMPANY_NAMES)
    rng.shuffle(companies)
    count = rng.randint(5, 9)
    rows: list[dict[str, object]] = []
    for i in range(count):
        company = companies[i % len(companies)]
        brief_id = i + 1
        status = BRIEF_PAYMENT_STATUSES[0] if brief_id == 2 else rng.choice(BRIEF_PAYMENT_STATUSES)
        created = now - timedelta(hours=rng.randint(2, 120), minutes=rng.randint(0, 50))
        paid_at: datetime | None = None
        session_id: str | None = None
        intent_id: str | None = None
        payment_subtotal_cents: int | None = None
        payment_discount_cents: int | None = None
        payment_amount_cents: int | None = None
        payment_currency: str | None = None
        stripe_promotion_code_id: str | None = None
        stripe_coupon_id: str | None = None
        # Keep id=1 full-price paid, id=2 unpaid+nullable, id=4 discounted paid.
        if brief_id == 1:
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        elif brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
            status = "paid"
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_discount_cents = 5_000
            payment_amount_cents = 15_000
            payment_currency = "usd"
            stripe_promotion_code_id = "promo_preview_25off"
            stripe_coupon_id = "coupon_preview_25off"
        elif status == "paid":
            paid_at = created + timedelta(minutes=rng.randint(5, 90))
            session_id = f"cs_preview_{rng.randint(100000, 999999)}"
            intent_id = f"pi_preview_{rng.randint(100000, 999999)}"
            payment_subtotal_cents = 20_000
            payment_amount_cents = 20_000
            payment_currency = "usd"
        utm_source = None if brief_id == 2 else rng.choice(UTM_SOURCES)
        utm_medium = None if brief_id == 2 else rng.choice(UTM_MEDIUMS)
        utm_campaign = None if brief_id == 2 else rng.choice(UTM_CAMPAIGNS)
        utm_content = None if brief_id == 2 else rng.choice(("cta-primary", "hero", None))
        utm_term = None if brief_id == 2 else rng.choice(("architecture", "cto", None))
        website = (
            "https://very-long-subdomain-name.example.co.uk/path/to/resource?query=value"
            if brief_id == 2
            else _brief_website(company, rng)
        )
        brief_text = (
            ("A" * 220)
            + "\n\nSecond paragraph with <script>alert(1)</script> for escape checks."
            if brief_id == 2
            else rng.choice(BRIEF_TEXTS)
        )
        rows.append(
            {
                "id": brief_id,
                "created_at": created,
                "website": website,
                "contact_method": "email",
                "contact_value": _brief_email(company, rng),
                "brief": brief_text,
                "status": status,
                "stripe_session_id": session_id,
                "stripe_payment_intent_id": intent_id,
                "paid_at": paid_at,
                "payment_subtotal_cents": payment_subtotal_cents,
                "payment_discount_cents": payment_discount_cents,
                "payment_amount_cents": payment_amount_cents,
                "payment_currency": payment_currency,
                "stripe_promotion_code_id": stripe_promotion_code_id,
                "stripe_coupon_id": stripe_coupon_id,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "utm_content": utm_content,
                "utm_term": utm_term,
            }
        )
    return rows


def build_preview_brief_detail(
    brief_id: int,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Return one preview brief by id (from the seeded list), or None if unknown."""
    if brief_id < 1:
        return None
    # Fresh rng from the same seed so list and detail stay consistent.
    list_rng = rng if rng is not None else _preview_rng()
    rows = build_preview_brief_rows(rng=list_rng, now=now)
    for row in rows:
        if int(row["id"]) == brief_id:  # type: ignore[arg-type]
            return row
    return None


def preview_pipeline_available() -> bool:
    return True


def preview_brief_conversion_state(brief_id: int) -> dict[str, object] | None:
    """Return linked CRM state for converted preview briefs."""
    if brief_id != PREVIEW_BRIEF_CONVERTED_ID:
        return None
    company_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    return {
        "company": {
            "id": company_id,
            "name": "Northwind Labs",
            "pipeline_stage": "diagnostic_paid",
        },
        "contact": {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "email": "alex.nguyen@northwindlabs.io",
        },
        "pipeline_stage": "diagnostic_paid",
    }


def preview_brief_convert_matches(
    brief_id: int,
    *,
    price_cents: int,
) -> dict[str, object]:
    from app.brief_conversion import build_conversion_proposal

    brief = build_preview_brief_detail(brief_id)
    if brief is None:
        return {"proposal": {}, "company_matches": [], "contact_matches": []}
    proposal = build_conversion_proposal(dict(brief), price_cents=price_cents)
    company_matches: list[dict[str, object]] = []
    contact_matches: list[dict[str, object]] = []
    if brief_id in (1, PREVIEW_BRIEF_CONVERT_MATCHES_ID):
        company_matches.append(
            {
                "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "name": "Northwind Labs (existing)",
                "domain": proposal.get("domain"),
            }
        )
    if brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
        contact_matches.append(
            {
                "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "email": proposal.get("contact_email"),
                "company_id": company_matches[0]["id"] if company_matches else None,
            }
        )
    return {
        "proposal": proposal,
        "company_matches": company_matches,
        "contact_matches": contact_matches,
    }


def preview_brief_convert_post(
    brief_id: int,
    *,
    company_mode: str,
    contact_mode: str,
    selected_company_id: object,
    selected_contact_id: object,
) -> str | None:
    """Simulate validation errors for preview POST; None means success."""
    if brief_id == PREVIEW_BRIEF_CONVERT_MATCHES_ID:
        if company_mode == "existing" and selected_company_id is None:
            return "Select an existing company match or choose to create a new company."
        if contact_mode == "existing" and selected_contact_id is None:
            return "Select the existing contact match or choose to create a new contact."
    if brief_id == PREVIEW_BRIEF_CONVERTED_ID:
        return None
    return None


def build_preview_company_detail(
    company_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    """Mock company detail data for Archive/Restore screenshot states."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    archived = company_id == PREVIEW_COMPANY_DETAIL_RESTORE_ID
    company_name = rng.choice(COMPANY_NAMES)
    company: dict[str, object] = {
        "id": str(company_id),
        "name": company_name,
        "domain": f"{company_name.lower().replace(' ', '')}.io",
        "category": rng.choice(tuple(COMPANY_CATEGORIES.keys())),
        "stage": rng.choice(tuple(COMPANY_STAGES.keys())),
        "target_status": "target",
        "headcount_estimate": rng.randint(12, 240),
        "funding_summary": f"Seed — ${rng.randint(2, 18)}M",
        "last_verified_at": (now - timedelta(days=rng.randint(3, 45))).date().isoformat(),
    }
    if archived:
        company["archived_at"] = (now - timedelta(days=rng.randint(7, 90))).isoformat()
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    contacts = [
        {
            "id": str(PREVIEW_CONTACT_DETAIL_ARCHIVE_ID),
            "full_name": f"{first} {last}",
            "title": "VP Engineering",
            "email": _slug_email(first, last, company_name, rng),
            "buying_roles": ["technical_buyer"],
        }
    ]
    records = [
        {
            "record_type": "verified_fact",
            "body": f"{company_name} raised a seed round and is hiring platform engineers.",
            "source_name": "Press release",
            "source_url": f"https://{company['domain']}/news/seed",
            "observed_value": "Seed announced",
            "observed_at": now - timedelta(days=rng.randint(1, 20)),
            "confidence": 0.9,
            "review_at": now + timedelta(days=30),
            "expires_at": now + timedelta(days=120),
        }
    ]
    return company, contacts, records


def build_preview_contact_detail(
    contact_id: UUID,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object] | None, list[dict[str, object]]]:
    """Mock contact detail/edit data for Archive/Restore screenshot states."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    archived = contact_id == PREVIEW_CONTACT_DETAIL_RESTORE_ID
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    company_name = rng.choice(COMPANY_NAMES)
    contact: dict[str, object] = {
        "id": str(contact_id),
        "full_name": f"{first} {last}",
        "title": "VP Engineering",
        "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
        "email": _slug_email(first, last, company_name, rng),
        "email_permission": "explicit_opt_in",
        "buying_roles": ["technical_buyer", "founder"],
        "relationship_strength": "warm",
        "last_interaction_at": (now - timedelta(days=rng.randint(2, 30))).date().isoformat(),
        "notes": "Met at fintech infra meetup; interested in architecture review.",
        "company_id": str(PREVIEW_COMPANY_DETAIL_ARCHIVE_ID),
    }
    if archived:
        contact["archived_at"] = (now - timedelta(days=rng.randint(7, 60))).isoformat()
    company = {
        "id": str(PREVIEW_COMPANY_DETAIL_ARCHIVE_ID),
        "name": company_name,
    }
    records = [
        {
            "record_type": "signal",
            "body": f"{first} posted about scaling payments infrastructure.",
            "source_name": "LinkedIn",
            "source_url": str(contact["profile_url"]),
            "observed_value": "Hiring signal",
            "observed_at": now - timedelta(days=rng.randint(1, 14)),
            "confidence": 0.75,
            "review_at": now + timedelta(days=21),
            "expires_at": now + timedelta(days=90),
        }
    ]
    return contact, company, records


def preview_contact_restore_conflict(
    *,
    rng: random.Random | None = None,
) -> dict[str, object]:
    """Mock archived/active pair for contact restore-conflict screenshots."""
    rng = rng or _preview_rng()
    first = rng.choice(CONTACT_FIRST)
    last = rng.choice(CONTACT_LAST)
    company = rng.choice(COMPANY_NAMES)
    email = _slug_email(first, last, company, rng)
    return {
        "archived_contact": {
            "id": str(PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID),
            "full_name": f"{first} {last}",
            "title": "Former VP Engineering",
            "email": email,
            "company_name": company,
            "archived_at": (
                datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=rng.randint(1, 30))
            ).isoformat(),
        },
        "conflicting_contact": {
            "contact_id": str(PREVIEW_CONTACT_RESTORE_CONFLICT_ACTIVE_ID),
            "full_name": f"{first} {last} (current)",
            "title": "CTO",
            "company_name": company,
            "company_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        },
    }


AUDIT_ACTIONS = (
    "admin.login.success",
    "admin.logout",
    "import.batch",
    "entity.delete",
    "pipeline.update",
    "brief.convert",
)


def build_preview_audit_events(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Randomized audit rows for ADMIN_PREVIEW_MODE screenshots."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    count = rng.randint(4, 8)
    events: list[dict[str, object]] = []
    for i in range(count):
        created = now - timedelta(hours=rng.randint(1, 72), minutes=rng.randint(0, 50))
        action = rng.choice(AUDIT_ACTIONS)
        company = rng.choice(COMPANY_NAMES)
        actor = f"{rng.choice(CONTACT_FIRST).lower()}@saberistic.com"
        events.append(
            {
                "id": i + 1,
                "created_at": created,
                "actor": actor,
                "action": action,
                "entity_type": "company" if "pipeline" in action or "delete" in action else None,
                "entity_id": str(rng.randint(10, 99)) if "pipeline" in action else None,
                "correlation_id": f"corr-preview-{rng.randint(1000, 9999)}",
                "summary_before": {"name": company} if "update" in action else None,
                "summary_after": {"pipeline_stage": rng.choice(list(PIPELINE_STAGES))}
                if "update" in action
                else {"ok": True},
            }
        )
    return events


@dataclass(frozen=True)
class PreviewLinkedInImportData:
    connection_count: int
    message_thread_count: int
    invitation_count: int
    company_follow_count: int
    duplicate_urls: tuple[str, ...]
    ignored_samples: tuple[str, ...]
    warnings: tuple[str, ...]


def build_preview_linkedin_import_data(
    *,
    rng: random.Random | None = None,
) -> PreviewLinkedInImportData:
    """Randomized LinkedIn import preview stats for ADMIN_PREVIEW_MODE."""
    rng = rng or _preview_rng()
    return PreviewLinkedInImportData(
        connection_count=rng.randint(120, 840),
        message_thread_count=rng.randint(8, 64),
        invitation_count=rng.randint(3, 40),
        company_follow_count=rng.randint(5, 55),
        duplicate_urls=tuple(
            f"https://linkedin.com/in/{rng.choice(CONTACT_FIRST).lower()}-{rng.choice(CONTACT_LAST).lower()}"
            for _ in range(rng.randint(1, 3))
        ),
        ignored_samples=(
            "Logins.csv",
            "PhoneNumbers.csv",
            "Security_Challenges.csv",
            "Ads Clicked.csv",
        ),
        warnings=(
            "Connections.csv: 2 rows missing profile URL",
            "messages.csv: 1 row without conversation id",
        ),
    )


def render_preview_imports_main(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """HTML main fragment for /admin/imports in preview mode (populated preview)."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    data = build_preview_linkedin_import_data(rng=rng)
    generated = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    dup_rows = "".join(
        f"<li>{html.escape(url)}</li>" for url in data.duplicate_urls
    )
    ignored_rows = "".join(
        f"<li><code>{html.escape(name)}</code></li>" for name in data.ignored_samples
    )
    warning_rows = "".join(
        f"<li>{html.escape(warning)}</li>" for warning in data.warnings
    )
    return f"""        <section class="admin-section linkedin-import" aria-labelledby="imports-title">
          <p class="admin-preview-banner" role="status">Preview data — not production</p>
          <div class="admin-section-head">
            <div>
              <p class="admin-eyebrow">Data import</p>
              <h1 class="admin-title" id="imports-title">LinkedIn export preview</h1>
            </div>
          </div>
          <p class="admin-lede">
            Mock parsed export for screenshots
            (<time datetime="{html.escape(generated)}">{html.escape(generated)}</time>).
            Parsing runs locally; nothing is uploaded.
          </p>
          <div class="linkedin-import-privacy" role="note">
            <h2 class="admin-section-title">Privacy &amp; retention</h2>
            <dl class="linkedin-import-privacy-grid">
              <div><dt>Processed locally</dt><dd><code>Connections.csv</code>, <code>messages.csv</code>, <code>Invitations.csv</code>, <code>Company Follows.csv</code></dd></div>
              <div><dt>Ignored in archive</dt><dd>Logins, phones, security challenges, ads, receipts, and all other files</dd></div>
              <div><dt>Transmitted</dt><dd>Nothing in this preview step</dd></div>
              <div><dt>Retained</dt><dd>Nothing server-side until a future import-batch step</dd></div>
            </dl>
          </div>
          <div class="linkedin-import-status linkedin-import-status--ok" role="status">Preview ready — nothing uploaded.</div>
          <div class="linkedin-import-preview">
            <h2 class="admin-section-title">Import preview</h2>
            <dl class="admin-stat-row linkedin-import-stats">
              <div><dt>Connections</dt><dd>{data.connection_count}</dd></div>
              <div><dt>Message threads</dt><dd>{data.message_thread_count}</dd></div>
              <div><dt>Invitations</dt><dd>{data.invitation_count}</dd></div>
              <div><dt>Company follows</dt><dd>{data.company_follow_count}</dd></div>
            </dl>
            <h3 class="admin-section-title">Proposed changes (preview only)</h3>
            <ul class="linkedin-import-proposed-list">
              <li><strong>New connections:</strong> {data.connection_count}</li>
              <li><strong>Message threads:</strong> {data.message_thread_count}</li>
              <li><strong>Invitations:</strong> {data.invitation_count}</li>
              <li><strong>Company follows:</strong> {data.company_follow_count}</li>
            </ul>
            <h3 class="admin-section-title">Validation warnings</h3>
            <ul class="linkedin-import-warnings">{warning_rows}</ul>
            <h3 class="admin-section-title">Duplicate profile URLs in export</h3>
            <ul class="linkedin-import-duplicates">{dup_rows}</ul>
            <h3 class="admin-section-title">Recognized files</h3>
            <div class="admin-table-wrap">
              <table class="admin-table">
                <thead><tr><th>File</th><th>Rows</th><th>Valid</th><th>Skipped</th><th>Path in archive</th></tr></thead>
                <tbody>
                  <tr><td>connections.csv</td><td>{data.connection_count + 2}</td><td>{data.connection_count}</td><td>2</td><td>LinkedIn Export/Connections.csv</td></tr>
                  <tr><td>messages.csv</td><td>{data.message_thread_count * 4}</td><td>{data.message_thread_count * 4 - 1}</td><td>1</td><td>LinkedIn Export/messages.csv</td></tr>
                  <tr><td>Invitations.csv</td><td>{data.invitation_count}</td><td>{data.invitation_count}</td><td>0</td><td>LinkedIn Export/Invitations.csv</td></tr>
                  <tr><td>company follows.csv</td><td>{data.company_follow_count}</td><td>{data.company_follow_count}</td><td>0</td><td>LinkedIn Export/Company Follows.csv</td></tr>
                </tbody>
              </table>
            </div>
            <h3 class="admin-section-title">Ignored archive entries</h3>
            <ul class="linkedin-import-ignored">{ignored_rows}</ul>
          </div>
        </section>"""


PREVIEW_IMPORT_BATCH_IDS = (
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
    UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"),
)


def build_preview_import_batches(
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Mock committed import batches for ADMIN_PREVIEW_MODE."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    batches: list[dict[str, object]] = []
    for index, batch_id in enumerate(PREVIEW_IMPORT_BATCH_IDS):
        created = now - timedelta(days=index + 1, hours=rng.randint(1, 8))
        batches.append(
            {
                "id": str(batch_id),
                "created_at": created,
                "updated_at": created,
                "source_type": "linkedin",
                "export_date": (created - timedelta(days=3)).date().isoformat(),
                "schema_version": "linkedin_export_v1",
                "checksum": f"preview-checksum-{index + 1:02d}{'a' * 52}",
                "actor": "preview-operator",
                "status": "committed" if index == 0 else "rolled_back",
                "summary_counts": {
                    "inserted": rng.randint(8, 24),
                    "updated": rng.randint(2, 9),
                    "unchanged": rng.randint(30, 90),
                    "skipped": rng.randint(0, 3),
                    "conflicted": 1 if index == 1 else 0,
                },
                "correlation_id": f"corr-preview-import-{index + 1}",
            }
        )
    return batches, len(batches)


def build_preview_import_batch_detail(
    batch_id: str,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Mock batch detail with representative row outcomes."""
    rng = rng or _preview_rng()
    batches, _ = build_preview_import_batches(rng=rng, now=now)
    batch = next((item for item in batches if str(item["id"]) == batch_id), None)
    if batch is None:
        return None
    rows: list[dict[str, object]] = []
    outcomes = ("inserted", "updated", "unchanged", "skipped", "conflicted")
    for index, outcome in enumerate(outcomes):
        company = rng.choice(COMPANY_NAMES)
        first = rng.choice(CONTACT_FIRST)
        last = rng.choice(CONTACT_LAST)
        rows.append(
            {
                "row_index": index,
                "source_kind": "linkedin_connection",
                "source_identity": {
                    "profile_url": f"https://linkedin.com/in/{first.lower()}-{last.lower()}",
                    "full_name": f"{first} {last}",
                    "company_name": company,
                    "title": rng.choice(("CTO", "VP Engineering", "Founder")),
                },
                "outcome": outcome,
                "entity_type": "contact" if outcome != "skipped" else None,
                "entity_id": str(UUID(int=rng.getrandbits(128), version=4))
                if outcome not in {"skipped", "conflicted"}
                else None,
                "detail": "Multiple contacts share this profile URL"
                if outcome == "conflicted"
                else ("Missing profile URL" if outcome == "skipped" else None),
            }
        )
    return {"batch": batch, "rows": rows}


def render_preview_section_main(
    *,
    label: str,
    summary: str,
    active_path: str,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> str:
    """HTML main fragment for an admin section page in preview mode."""
    rng = rng or _preview_rng()
    now = now or datetime.now(timezone.utc)
    columns = _SECTION_COLUMNS.get(
        active_path, ("Item", "Detail", "Owner", "Status", "Updated")
    )
    data_rows = build_preview_section_rows(active_path, rng=rng, now=now)
    head = "".join(f"<th scope=\"col\">{html.escape(col)}</th>" for col in columns)
    body = "\n".join(
        "<tr>"
        + "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
        + "</tr>"
        for row in data_rows
    )
    generated = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    safe_label = html.escape(label)
    return f"""        <section class="admin-empty" aria-labelledby="admin-section-title">
          <p class="admin-preview-banner" role="status">Preview data — not production</p>
          <p class="admin-eyebrow">Preview</p>
          <h1 class="admin-title" id="admin-section-title">{safe_label}</h1>
          <p class="admin-lede">
            {html.escape(summary)} Mock rows for screenshots
            (<time datetime="{html.escape(generated)}">{html.escape(generated)}</time>).
          </p>
          <div class="admin-table-wrap">
            <table class="admin-table">
              <thead>
                <tr>
                  {head}
                </tr>
              </thead>
              <tbody>
                {body}
              </tbody>
            </table>
          </div>
        </section>"""

