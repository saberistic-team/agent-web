"""Incremental LinkedIn connection reconciliation with field-level source precedence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID

from app.contacts import normalize_contact_name, normalize_email
from app.linkedin_import import (
    SOURCE_LINKEDIN,
    SOURCE_MANUAL,
    normalize_connection_row,
    parse_export_date,
)

MatchTier = Literal["profile_url", "email", "name_company", "none"]
PreviewOutcome = Literal["insert", "update", "unchanged", "conflict", "skipped"]
CommitOutcome = Literal["inserted", "updated", "unchanged", "conflicted", "skipped"]

LINKEDIN_IMPORTABLE_FIELDS = (
    "full_name",
    "title",
    "profile_url",
    "email",
    "company_id",
    "last_interaction_at",
)
USER_PROTECTED_FIELDS = frozenset(
    {"notes", "relationship_strength", "buying_roles", "crm_context_tags"}
)


@dataclass(frozen=True)
class FieldSource:
    source: str
    batch_id: str | None = None
    seen_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "batch_id": self.batch_id,
            "seen_at": self.seen_at,
        }


@dataclass(frozen=True)
class MatchCandidate:
    contact_id: str
    full_name: str | None
    title: str | None
    company_name: str | None
    profile_url: str | None
    email: str | None


@dataclass
class MatchResolution:
    tier: MatchTier = "none"
    contact: dict[str, Any] | None = None
    conflict: bool = False
    reason: str | None = None
    candidates: list[MatchCandidate] = field(default_factory=list)


@dataclass
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass
class ReconcilePreviewRow:
    row_index: int
    outcome: PreviewOutcome
    identity: dict[str, Any]
    match_tier: MatchTier
    contact_id: str | None = None
    contact_label: str | None = None
    field_changes: list[FieldChange] = field(default_factory=list)
    conflict_reason: str | None = None
    conflict_candidates: list[MatchCandidate] = field(default_factory=list)
    detail: str | None = None


@dataclass
class ReconcilePreview:
    rows: list[ReconcilePreviewRow]
    summary_counts: dict[str, int]
    absent_preserved: int = 0


def parse_field_sources(raw: Any) -> dict[str, dict[str, Any]]:
    if not raw or not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def is_field_user_owned(field_sources: dict[str, dict[str, Any]], field: str) -> bool:
    entry = field_sources.get(field)
    if entry and entry.get("source") == SOURCE_MANUAL:
        return True
    if field in USER_PROTECTED_FIELDS and entry and entry.get("source") != SOURCE_LINKEDIN:
        return True
    return False


def email_match_permitted(contact: dict[str, Any] | None, import_email: str | None) -> bool:
    if not import_email:
        return False
    if contact is None:
        return True
    permission = contact.get("email_permission")
    if permission == "restricted":
        return False
    contact_email = contact.get("email")
    if not contact_email:
        return True
    try:
        return normalize_email(str(contact_email)) == normalize_email(import_email)
    except ValueError:
        return False


def _candidate_from_contact(contact: dict[str, Any]) -> MatchCandidate:
    return MatchCandidate(
        contact_id=str(contact["id"]),
        full_name=contact.get("full_name"),
        title=contact.get("title"),
        company_name=contact.get("company_name"),
        profile_url=contact.get("profile_url"),
        email=contact.get("email"),
    )


def resolve_company_id(
    company_name: str | None,
    *,
    companies_by_name: dict[str, list[dict[str, Any]]],
) -> tuple[UUID | None, list[dict[str, Any]]]:
    if not company_name:
        return None, []
    key = company_name.strip().lower()
    matches = companies_by_name.get(key, [])
    if len(matches) == 1:
        return UUID(str(matches[0]["id"])), matches
    return None, matches


def resolve_connection_match(
    identity: dict[str, Any],
    *,
    profile_matches: list[dict[str, Any]],
    email_match: dict[str, Any] | None,
    name_company_matches: list[dict[str, Any]],
    company_ambiguity: list[dict[str, Any]] | None = None,
) -> MatchResolution:
    profile_url = identity.get("profile_url")
    if profile_url:
        if len(profile_matches) == 1:
            return MatchResolution(tier="profile_url", contact=profile_matches[0])
        if len(profile_matches) > 1:
            return MatchResolution(
                tier="profile_url",
                conflict=True,
                reason="Multiple contacts share this profile URL",
                candidates=[_candidate_from_contact(item) for item in profile_matches],
            )

    import_email = identity.get("email")
    if import_email and email_match is not None and email_match_permitted(email_match, import_email):
        return MatchResolution(tier="email", contact=email_match)

    if company_ambiguity and len(company_ambiguity) > 1:
        return MatchResolution(
            tier="name_company",
            conflict=True,
            reason="Ambiguous company name for name/company match",
            candidates=[
                MatchCandidate(
                    contact_id=str(item["id"]),
                    full_name=None,
                    title=None,
                    company_name=item.get("name"),
                    profile_url=None,
                    email=None,
                )
                for item in company_ambiguity
            ],
        )

    if identity.get("full_name") and identity.get("company_name"):
        if len(name_company_matches) == 1:
            return MatchResolution(tier="name_company", contact=name_company_matches[0])
        if len(name_company_matches) > 1:
            return MatchResolution(
                tier="name_company",
                conflict=True,
                reason="Multiple contacts match name and company",
                candidates=[_candidate_from_contact(item) for item in name_company_matches],
            )

    return MatchResolution(tier="none", contact=None)


def linkedin_field_stamp(*, batch_id: str, seen_at: datetime | None = None) -> dict[str, Any]:
    moment = seen_at or datetime.now(timezone.utc)
    return FieldSource(
        source=SOURCE_LINKEDIN,
        batch_id=batch_id,
        seen_at=moment.isoformat(),
    ).as_dict()


def compute_importable_updates(
    contact: dict[str, Any] | None,
    identity: dict[str, Any],
    *,
    company_id: UUID | None,
    batch_id: str,
    seen_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[FieldChange]]:
    field_sources = parse_field_sources(contact.get("field_sources") if contact else None)
    updates: dict[str, Any] = {}
    new_sources = dict(field_sources)
    changes: list[FieldChange] = []
    stamp = linkedin_field_stamp(batch_id=batch_id, seen_at=seen_at)

    connected = parse_export_date(identity.get("connected_on"))

    desired: dict[str, Any] = {
        "full_name": identity.get("full_name"),
        "title": identity.get("title"),
        "profile_url": identity.get("profile_url"),
        "email": identity.get("email"),
        "company_id": company_id,
        "last_interaction_at": connected,
    }

    for field_name, desired_value in desired.items():
        if field_name not in LINKEDIN_IMPORTABLE_FIELDS:
            continue
        if is_field_user_owned(field_sources, field_name):
            continue
        if field_name == "email" and contact is not None:
            if not email_match_permitted(contact, identity.get("email")):
                continue
            if desired_value and not contact.get("email"):
                updates["email_permission"] = "inferred"
        current_value = contact.get(field_name) if contact else None
        if desired_value is None or desired_value == "":
            continue
        if str(current_value or "") == str(desired_value or ""):
            continue
        if contact is None or current_value in (None, "") or field_sources.get(field_name, {}).get("source") == SOURCE_LINKEDIN:
            updates[field_name] = desired_value
            new_sources[field_name] = stamp
            changes.append(
                FieldChange(
                    field=field_name,
                    before=current_value,
                    after=desired_value,
                )
            )

    return updates, new_sources, changes


def preview_connection_row(
    *,
    row_index: int,
    raw_row: dict[str, Any],
    match: MatchResolution,
    company_id: UUID | None,
    batch_id: str,
    seen_at: datetime | None = None,
) -> ReconcilePreviewRow:
    identity = normalize_connection_row(raw_row)
    if not identity.get("profile_url") and not identity.get("email") and not identity.get("full_name"):
        return ReconcilePreviewRow(
            row_index=row_index,
            outcome="skipped",
            identity=identity,
            match_tier="none",
            detail="Missing profile URL, email, and name",
        )

    if match.conflict:
        return ReconcilePreviewRow(
            row_index=row_index,
            outcome="conflict",
            identity=identity,
            match_tier=match.tier,
            conflict_reason=match.reason,
            conflict_candidates=list(match.candidates),
        )

    contact = match.contact
    updates, _, changes = compute_importable_updates(
        contact,
        identity,
        company_id=company_id,
        batch_id=batch_id,
        seen_at=seen_at,
    )

    if contact is None:
        return ReconcilePreviewRow(
            row_index=row_index,
            outcome="insert",
            identity=identity,
            match_tier=match.tier,
            field_changes=changes or [
                FieldChange(field=key, before=None, after=value) for key, value in updates.items()
            ],
        )

    if not changes:
        return ReconcilePreviewRow(
            row_index=row_index,
            outcome="unchanged",
            identity=identity,
            match_tier=match.tier,
            contact_id=str(contact["id"]),
            contact_label=str(contact.get("full_name") or contact.get("email") or contact["id"]),
        )

    return ReconcilePreviewRow(
        row_index=row_index,
        outcome="update",
        identity=identity,
        match_tier=match.tier,
        contact_id=str(contact["id"]),
        contact_label=str(contact.get("full_name") or contact.get("email") or contact["id"]),
        field_changes=changes,
    )


def build_reconcile_preview(
    connections: list[dict[str, Any]],
    *,
    lookup: Any,
    batch_id: str,
    seen_at: datetime | None = None,
    existing_contact_count: int = 0,
) -> ReconcilePreview:
    """Build a dry-run preview; ``lookup`` resolves matches per normalized identity."""
    rows: list[ReconcilePreviewRow] = []
    summary = {
        "insert": 0,
        "update": 0,
        "unchanged": 0,
        "conflict": 0,
        "skipped": 0,
    }
    for index, raw_row in enumerate(connections):
        identity = normalize_connection_row(raw_row)
        match, company_id = lookup(identity)
        preview_row = preview_connection_row(
            row_index=index,
            raw_row=raw_row,
            match=match,
            company_id=company_id,
            batch_id=batch_id,
            seen_at=seen_at,
        )
        rows.append(preview_row)
        summary[preview_row.outcome] += 1

    touched_ids = {
        row.contact_id for row in rows if row.contact_id and row.outcome in {"update", "unchanged"}
    }
    absent_preserved = max(existing_contact_count - len(touched_ids), 0)

    return ReconcilePreview(
        rows=rows,
        summary_counts=summary,
        absent_preserved=absent_preserved,
    )


def preview_row_to_dict(row: ReconcilePreviewRow) -> dict[str, Any]:
    return {
        "row_index": row.row_index,
        "outcome": row.outcome,
        "identity": row.identity,
        "match_tier": row.match_tier,
        "contact_id": row.contact_id,
        "contact_label": row.contact_label,
        "field_changes": [
            {"field": change.field, "before": change.before, "after": change.after}
            for change in row.field_changes
        ],
        "conflict_reason": row.conflict_reason,
        "conflict_candidates": [
            {
                "contact_id": candidate.contact_id,
                "full_name": candidate.full_name,
                "title": candidate.title,
                "company_name": candidate.company_name,
                "profile_url": candidate.profile_url,
                "email": candidate.email,
            }
            for candidate in row.conflict_candidates
        ],
        "detail": row.detail,
    }


def preview_to_dict(preview: ReconcilePreview) -> dict[str, Any]:
    return {
        "rows": [preview_row_to_dict(row) for row in preview.rows],
        "summary_counts": preview.summary_counts,
        "absent_preserved": preview.absent_preserved,
    }


def index_companies_by_name(companies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for company in companies:
        name = normalize_contact_name(company.get("name"))
        if not name:
            continue
        key = name.lower()
        index.setdefault(key, []).append(company)
    return index
