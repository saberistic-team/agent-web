"""LinkedIn data-export ZIP parsing and preview (reference implementation).

Production parsing runs in the browser via ``site/assets/linkedin-export.js``.
This module mirrors those limits and behavior for unit tests and fixtures.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.contacts import normalize_email, normalize_profile_url

# Security limits — keep in sync with site/assets/linkedin-export.js (see tests).
MAX_COMPRESSED_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 500
MAX_PATH_DEPTH = 3
MAX_PATH_LENGTH = 255
MAX_CSV_ROWS = 100_000
MAX_FIELD_LENGTH = 10_000
MAX_DUPLICATE_SAMPLES = 20
MAX_PROPOSED_SAMPLE = 50
MAX_IGNORED_SAMPLES = 15

NESTED_ARCHIVE_SUFFIXES = (
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
)

APPROVED_BASENAMES: dict[str, str] = {
    "connections.csv": "connections",
    "messages.csv": "messages",
    "invitations.csv": "invitations",
    "company follows.csv": "company_follows",
}

SENSITIVE_IGNORED_HINTS = (
    "login",
    "security",
    "challenge",
    "phone",
    "job",
    "answer",
    "ad",
    "verification",
    "receipt",
)

_CONNECTIONS_REQUIRED = frozenset({"first name", "last name", "url"})
_MESSAGES_REQUIRED = frozenset({"conversation id", "from", "to", "date"})
_INVITATIONS_REQUIRED = frozenset({"from", "to", "sent at"})
_COMPANY_FOLLOWS_REQUIRED = frozenset({"organization"})


@dataclass(frozen=True)
class RecognizedFile:
    basename: str
    kind: str
    row_count: int
    valid_rows: int
    skipped_rows: int


@dataclass
class LinkedInExportPreview:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recognized_files: list[RecognizedFile] = field(default_factory=list)
    ignored_file_count: int = 0
    ignored_file_samples: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    proposed_changes: list[dict[str, Any]] = field(default_factory=list)
    messages_redacted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "recognized_files": [
                {
                    "basename": f.basename,
                    "kind": f.kind,
                    "row_count": f.row_count,
                    "valid_rows": f.valid_rows,
                    "skipped_rows": f.skipped_rows,
                }
                for f in self.recognized_files
            ],
            "ignored_file_count": self.ignored_file_count,
            "ignored_file_samples": list(self.ignored_file_samples),
            "counts": dict(self.counts),
            "duplicates": list(self.duplicates),
            "proposed_changes": list(self.proposed_changes),
            "messages_redacted": self.messages_redacted,
        }


def export_limits() -> dict[str, int | tuple[str, ...]]:
    """Return limit constants for cross-checking the browser parser."""
    return {
        "MAX_COMPRESSED_BYTES": MAX_COMPRESSED_BYTES,
        "MAX_UNCOMPRESSED_BYTES": MAX_UNCOMPRESSED_BYTES,
        "MAX_ARCHIVE_ENTRIES": MAX_ARCHIVE_ENTRIES,
        "MAX_PATH_DEPTH": MAX_PATH_DEPTH,
        "MAX_PATH_LENGTH": MAX_PATH_LENGTH,
        "MAX_CSV_ROWS": MAX_CSV_ROWS,
        "MAX_FIELD_LENGTH": MAX_FIELD_LENGTH,
        "NESTED_ARCHIVE_SUFFIXES": NESTED_ARCHIVE_SUFFIXES,
    }


def _normalize_basename(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    return normalized.rsplit("/", 1)[-1].lower()


def _path_depth(path: str) -> int:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return 0
    return len([part for part in normalized.split("/") if part])


def _validate_archive_path(path: str) -> str | None:
    if not path or len(path) > MAX_PATH_LENGTH:
        return "Archive entry path exceeds length limit"
    if "\x00" in path:
        return "Archive entry path contains null bytes"
    if path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
        return "Archive entry path must be relative"
    if ".." in path.replace("\\", "/").split("/"):
        return "Archive entry path traversal is not allowed"
    if _path_depth(path) > MAX_PATH_DEPTH:
        return "Archive entry path exceeds nesting depth"
    lower = path.lower()
    if any(lower.endswith(suffix) for suffix in NESTED_ARCHIVE_SUFFIXES):
        return "Nested archives are not allowed"
    return None


def _normalize_headers(row: list[str]) -> list[str]:
    return [cell.strip().lower() for cell in row]


def _validate_headers(headers: list[str], required: frozenset[str]) -> str | None:
    header_set = frozenset(headers)
    missing = required - header_set
    if missing:
        return f"Missing required columns: {', '.join(sorted(missing))}"
    return None


def _truncate_field(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_FIELD_LENGTH:
        return value, False
    return value[:MAX_FIELD_LENGTH], True


def _read_csv_rows(
    raw: bytes,
    *,
    required_headers: frozenset[str],
) -> tuple[list[dict[str, str]], int, int, list[str]]:
    warnings: list[str] = []
    skipped = 0
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], 0, 0, ["CSV is not valid UTF-8"]
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return [], 0, 0, ["CSV is empty"]
    headers = _normalize_headers(header_row)
    schema_error = _validate_headers(headers, required_headers)
    if schema_error:
        return [], 0, 0, [schema_error]
    rows: list[dict[str, str]] = []
    for row_index, row in enumerate(reader, start=2):
        if row_index - 1 > MAX_CSV_ROWS:
            warnings.append(f"Row limit ({MAX_CSV_ROWS}) reached; remaining rows ignored")
            break
        if not row or all(not cell.strip() for cell in row):
            skipped += 1
            continue
        if len(row) != len(headers):
            skipped += 1
            continue
        record: dict[str, str] = {}
        truncated = False
        for header, cell in zip(headers, row, strict=True):
            value, was_truncated = _truncate_field(cell.strip())
            truncated = truncated or was_truncated
            record[header] = value
        if truncated:
            warnings.append(f"Row {row_index}: one or more fields truncated to {MAX_FIELD_LENGTH} chars")
        rows.append(record)
    return rows, len(rows) + skipped, skipped, warnings


def _parse_connections(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    proposed: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen_urls: dict[str, int] = {}
    skipped = 0
    for row in rows:
        first = row.get("first name", "").strip()
        last = row.get("last name", "").strip()
        url_raw = row.get("url", "").strip()
        if not first and not last and not url_raw:
            skipped += 1
            continue
        profile_url: str | None
        try:
            profile_url = normalize_profile_url(url_raw) if url_raw else None
        except ValueError:
            skipped += 1
            continue
        email: str | None = None
        email_raw = row.get("email address", "").strip()
        if email_raw:
            try:
                email = normalize_email(email_raw)
            except ValueError:
                pass
        name = " ".join(part for part in (first, last) if part).strip() or "Unknown"
        change = {
            "kind": "contact",
            "name": name,
            "company": row.get("company", "").strip() or None,
            "title": row.get("position", "").strip() or None,
            "profile_url": profile_url,
            "email": email,
            "connected_on": row.get("connected on", "").strip() or None,
            "source_file": "connections.csv",
        }
        if profile_url:
            if profile_url in seen_urls:
                duplicates.append(
                    {
                        "kind": "connection_profile_url",
                        "value": profile_url,
                        "first_row": seen_urls[profile_url],
                    }
                )
            else:
                seen_urls[profile_url] = len(proposed) + 1
        if len(proposed) < MAX_PROPOSED_SAMPLE:
            proposed.append(change)
    return proposed, duplicates, skipped


def _parse_messages(rows: list[dict[str, str]]) -> tuple[dict[str, int], int]:
    conversations: set[str] = set()
    skipped = 0
    for row in rows:
        conv_id = row.get("conversation id", "").strip()
        if not conv_id:
            skipped += 1
            continue
        conversations.add(conv_id)
    return {"conversations": len(conversations), "messages": len(rows) - skipped}, skipped


def _parse_invitations(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int]:
    proposed: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        sender = row.get("from", "").strip()
        recipient = row.get("to", "").strip()
        if not sender and not recipient:
            skipped += 1
            continue
        if len(proposed) < MAX_PROPOSED_SAMPLE:
            proposed.append(
                {
                    "kind": "invitation",
                    "from": sender or None,
                    "to": recipient or None,
                    "sent_at": row.get("sent at", "").strip() or None,
                    "direction": row.get("direction", "").strip() or None,
                    "source_file": "invitations.csv",
                }
            )
    return proposed, skipped


def _parse_company_follows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    proposed: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen_orgs: dict[str, int] = {}
    skipped = 0
    for row in rows:
        org = row.get("organization", "").strip()
        if not org:
            skipped += 1
            continue
        key = org.lower()
        if key in seen_orgs:
            duplicates.append(
                {
                    "kind": "company_follow",
                    "value": org,
                    "first_row": seen_orgs[key],
                }
            )
        else:
            seen_orgs[key] = len(proposed) + 1
        if len(proposed) < MAX_PROPOSED_SAMPLE:
            proposed.append(
                {
                    "kind": "company_follow",
                    "organization": org,
                    "followed_on": row.get("followed on", "").strip() or None,
                    "source_file": "company follows.csv",
                }
            )
    return proposed, duplicates, skipped


def parse_linkedin_export_zip(data: bytes) -> LinkedInExportPreview:
    """Parse a LinkedIn export ZIP and return a safe preview summary."""
    preview = LinkedInExportPreview(ok=False)
    if len(data) > MAX_COMPRESSED_BYTES:
        preview.errors.append(
            f"Compressed archive exceeds {MAX_COMPRESSED_BYTES // (1024 * 1024)} MB limit"
        )
        return preview

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        preview.errors.append("File is not a valid ZIP archive")
        return preview

    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        preview.errors.append(f"Archive contains more than {MAX_ARCHIVE_ENTRIES} entries")
        return preview

    total_uncompressed = 0
    approved_payloads: dict[str, bytes] = {}
    ignored_names: list[str] = []

    for info in entries:
        path = info.filename
        path_error = _validate_archive_path(path)
        if path_error:
            preview.errors.append(f"{path}: {path_error}")
            return preview
        if info.is_dir():
            continue
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            preview.errors.append(
                f"Uncompressed archive exceeds {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit"
            )
            return preview
        basename = _normalize_basename(path)
        if basename in APPROVED_BASENAMES:
            if basename in approved_payloads:
                preview.warnings.append(f"Duplicate approved file ignored: {basename}")
                continue
            try:
                approved_payloads[basename] = archive.read(path)
            except RuntimeError as exc:
                preview.errors.append(f"Failed to read {path}: {exc}")
                return preview
        else:
            ignored_names.append(path)

    preview.ignored_file_count = len(ignored_names)
    preview.ignored_file_samples = ignored_names[:MAX_IGNORED_SAMPLES]

    counts: dict[str, int] = {
        "connections": 0,
        "messages": 0,
        "conversations": 0,
        "invitations": 0,
        "company_follows": 0,
    }
    all_proposed: list[dict[str, Any]] = []
    all_duplicates: list[dict[str, Any]] = []

    parsers: dict[str, tuple[frozenset[str], str]] = {
        "connections.csv": (_CONNECTIONS_REQUIRED, "connections"),
        "messages.csv": (_MESSAGES_REQUIRED, "messages"),
        "invitations.csv": (_INVITATIONS_REQUIRED, "invitations"),
        "company follows.csv": (_COMPANY_FOLLOWS_REQUIRED, "company_follows"),
    }

    for basename, (required, kind) in parsers.items():
        if basename not in approved_payloads:
            continue
        rows, row_count, skipped, row_warnings = _read_csv_rows(
            approved_payloads[basename],
            required_headers=required,
        )
        preview.warnings.extend(row_warnings)
        if not rows and row_warnings and row_warnings[0].startswith("Missing required"):
            preview.errors.append(f"{basename}: {row_warnings[0]}")
            continue
        valid_rows = len(rows)
        preview.recognized_files.append(
            RecognizedFile(
                basename=basename,
                kind=kind,
                row_count=row_count,
                valid_rows=valid_rows,
                skipped_rows=skipped,
            )
        )
        if kind == "connections":
            proposed, dups, parse_skipped = _parse_connections(rows)
            counts["connections"] = valid_rows - parse_skipped
            all_proposed.extend(proposed)
            all_duplicates.extend(dups[:MAX_DUPLICATE_SAMPLES])
        elif kind == "messages":
            msg_counts, parse_skipped = _parse_messages(rows)
            counts["messages"] = msg_counts["messages"]
            counts["conversations"] = msg_counts["conversations"]
            if parse_skipped:
                preview.warnings.append(
                    f"messages.csv: skipped {parse_skipped} rows without conversation id"
                )
        elif kind == "invitations":
            proposed, parse_skipped = _parse_invitations(rows)
            counts["invitations"] = valid_rows - parse_skipped
            all_proposed.extend(proposed)
        elif kind == "company_follows":
            proposed, dups, parse_skipped = _parse_company_follows(rows)
            counts["company_follows"] = valid_rows - parse_skipped
            all_proposed.extend(proposed)
            all_duplicates.extend(dups[:MAX_DUPLICATE_SAMPLES])

    if not approved_payloads:
        preview.errors.append("No recognized LinkedIn export files found in archive")
        return preview

    if preview.errors:
        return preview

    preview.counts = counts
    preview.proposed_changes = all_proposed
    preview.duplicates = all_duplicates[:MAX_DUPLICATE_SAMPLES]
    preview.ok = True
    preview.messages_redacted = True

    sensitive_ignored = [
        name
        for name in ignored_names
        if any(hint in _normalize_basename(name) for hint in SENSITIVE_IGNORED_HINTS)
    ]
    if sensitive_ignored:
        preview.warnings.append(
            f"Ignored {len(sensitive_ignored)} sensitive file(s) (logins, security, phones, etc.)"
        )

    return preview


def build_zip_bytes(files: dict[str, str | bytes]) -> bytes:
    """Build a ZIP archive for tests. Values are UTF-8 text or raw bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            payload = content.encode("utf-8") if isinstance(content, str) else content
            archive.writestr(path, payload)
    return buffer.getvalue()
