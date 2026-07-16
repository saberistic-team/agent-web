"""Safe parsing of official LinkedIn data-export ZIP archives.

Used by unit tests and as the canonical spec for browser-side parsing in
``site/assets/linkedin-import.js``.  Raw message body text is counted but
never returned in preview payloads.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.contacts import normalize_profile_url

# --- Limits (mirrored in linkedin-import.js) --------------------------------

MAX_COMPRESSED_BYTES = 52_428_800  # 50 MiB
MAX_UNCOMPRESSED_BYTES = 209_715_200  # 200 MiB
MAX_ZIP_ENTRIES = 500
MAX_CSV_ROWS = 50_000
MAX_FIELD_LENGTH = 10_000
MAX_PATH_LENGTH = 512
MAX_PREAMBLE_SCAN_LINES = 20

APPROVED_BASENAMES: frozenset[str] = frozenset(
    {
        "connections.csv",
        "messages.csv",
        "invitations.csv",
        "company follows.csv",
    }
)

NESTED_ARCHIVE_SUFFIXES: frozenset[str] = frozenset(
    {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
)

_IGNORED_EXPORT_HINTS: frozenset[str] = frozenset(
    {
        "logins",
        "login",
        "phone",
        "security",
        "challenge",
        "job",
        "ads",
        "ad ",
        "verification",
        "receipt",
        "email addresses",
        "skills",
        "positions",
        "education",
        "certifications",
        "recommendations",
        "rich_media",
        "profile",
        "registration",
        "saved",
        "search",
        "endorsement",
        "language",
        "learning",
        "hashtag",
        "instant",
        "member",
        "organization",
        "project",
        "publication",
        "volunteer",
    }
)

# Minimum header tokens per approved file (case-insensitive substring match).
_REQUIRED_HEADER_TOKENS: dict[str, tuple[str, ...]] = {
    "connections.csv": ("first name", "url"),
    "messages.csv": ("conversation", "date"),
    "invitations.csv": ("from", "sent"),
    "company follows.csv": ("organization", "followed"),
}


@dataclass(frozen=True)
class ParsedFileSummary:
    basename: str
    archive_path: str
    row_count: int
    valid_rows: int
    skipped_rows: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkedInExportPreview:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    ignored_files: tuple[str, ...] = ()
    files: tuple[ParsedFileSummary, ...] = ()
    connection_count: int = 0
    message_thread_count: int = 0
    message_row_count: int = 0
    invitation_count: int = 0
    company_follow_count: int = 0
    duplicate_profile_urls: tuple[str, ...] = ()
    proposed_changes: dict[str, int] = field(default_factory=dict)


def _normalize_basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _is_safe_zip_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    if not normalized or len(normalized) > MAX_PATH_LENGTH:
        return False
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    parts = normalized.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _looks_like_ignored_export_file(path: str) -> bool:
    base = _normalize_basename(path)
    if base in APPROVED_BASENAMES:
        return False
    lower = base.lower()
    return any(hint in lower for hint in _IGNORED_EXPORT_HINTS)


def _is_nested_archive(path: str) -> bool:
    base = _normalize_basename(path)
    return any(base.endswith(suffix) for suffix in NESTED_ARCHIVE_SUFFIXES)


def _split_csv_lines(text: str) -> list[str]:
    """Split CSV text into physical lines, respecting quoted newlines."""
    lines: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            if in_quotes and index + 1 < length and text[index + 1] == '"':
                current.append('"')
                index += 2
                continue
            in_quotes = not in_quotes
            current.append(char)
        elif char in "\n\r" and not in_quotes:
            if char == "\r" and index + 1 < length and text[index + 1] == "\n":
                index += 1
            lines.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if current:
        lines.append("".join(current))
    return lines


def _count_csv_fields(line: str) -> int:
    if not line.strip():
        return 0
    return len(next(csv.reader([line])))


def _find_csv_header_index(lines: list[str]) -> int | None:
    """Locate the real header row, skipping blank and single-field preamble lines."""
    limit = min(len(lines), MAX_PREAMBLE_SCAN_LINES)
    for index in range(limit):
        if not lines[index].strip():
            continue
        if _count_csv_fields(lines[index]) > 1:
            return index
    return None


def _read_csv_rows(raw: bytes, *, basename: str) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    warnings: list[str] = []
    text = raw.decode("utf-8-sig", errors="replace")
    if "\x00" in text:
        raise ValueError(f"{basename}: binary content is not valid CSV")

    lines = _split_csv_lines(text)
    if not lines:
        raise ValueError(f"{basename}: missing CSV header row")

    header_index = _find_csv_header_index(lines)
    if header_index is None:
        raise ValueError(f"{basename}: missing CSV header row")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    if reader.fieldnames is None:
        raise ValueError(f"{basename}: missing CSV header row")

    header_lower = {name.strip().lower(): name for name in reader.fieldnames if name}
    required = _REQUIRED_HEADER_TOKENS.get(basename, ())
    for token in required:
        if not any(token in key for key in header_lower):
            warnings.append(f"{basename}: unexpected schema (missing '{token}' column)")

    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader, start=2):
        if index - 1 > MAX_CSV_ROWS:
            warnings.append(f"{basename}: truncated at {MAX_CSV_ROWS:,} rows")
            break
        line_number = header_index + index
        cleaned: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            cell = "" if value is None else str(value)
            if len(cell) > MAX_FIELD_LENGTH:
                warnings.append(
                    f"{basename}: row {line_number} field '{key}' exceeds max length; truncated"
                )
                cell = cell[:MAX_FIELD_LENGTH]
            cleaned[key.strip()] = cell
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows, tuple(warnings)


def _connection_profile_url(row: dict[str, str]) -> str | None:
    for key, value in row.items():
        if key.lower() in {"url", "profile url", "linkedin url"} and value.strip():
            try:
                return normalize_profile_url(value)
            except ValueError:
                return None
    return None


def _parse_connections(rows: list[dict[str, str]]) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
    valid = 0
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    warnings: list[str] = []
    for row in rows:
        url = _connection_profile_url(row)
        if url is None:
            continue
        valid += 1
        seen[url] = seen.get(url, 0) + 1
        if seen[url] == 2:
            duplicates.append(url)
    if valid == 0 and rows:
        warnings.append("Connections.csv: no rows with a recognizable profile URL")
    return valid, len(rows) - valid, tuple(duplicates), tuple(warnings)


def _parse_messages(rows: list[dict[str, str]]) -> tuple[int, int, tuple[str, ...]]:
    """Return (thread_count, valid_rows, warnings). Never expose message bodies."""
    warnings: list[str] = []
    threads: set[str] = set()
    valid = 0
    for row in rows:
        conv_id = ""
        for key, value in row.items():
            lower = key.lower()
            if "conversation" in lower and "id" in lower:
                conv_id = value.strip()
                break
        if conv_id:
            threads.add(conv_id)
            valid += 1
    if rows and valid == 0:
        warnings.append("messages.csv: no rows with a conversation id")
    return len(threads), valid, tuple(warnings)


def _parse_invitations(rows: list[dict[str, str]]) -> tuple[int, int, tuple[str, ...]]:
    valid = 0
    warnings: list[str] = []
    for row in rows:
        if any(value.strip() for value in row.values()):
            valid += 1
    return valid, len(rows) - valid, tuple(warnings)


def _parse_company_follows(rows: list[dict[str, str]]) -> tuple[int, int, tuple[str, ...]]:
    valid = 0
    warnings: list[str] = []
    for row in rows:
        org = ""
        for key, value in row.items():
            if "organization" in key.lower() or key.lower() == "company":
                org = value.strip()
                break
        if org:
            valid += 1
    if rows and valid == 0:
        warnings.append("Company Follows.csv: no rows with an organization name")
    return valid, len(rows) - valid, tuple(warnings)


def parse_linkedin_export_zip(data: bytes) -> LinkedInExportPreview:
    """Parse a LinkedIn export ZIP and return a safe preview summary."""
    errors: list[str] = []
    warnings: list[str] = []
    ignored: list[str] = []
    summaries: list[ParsedFileSummary] = []

    if len(data) > MAX_COMPRESSED_BYTES:
        return LinkedInExportPreview(
            ok=False,
            errors=(f"Archive exceeds {MAX_COMPRESSED_BYTES // (1024 * 1024)} MiB compressed limit.",),
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return LinkedInExportPreview(ok=False, errors=("File is not a valid ZIP archive.",))

    entries = zf.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        zf.close()
        return LinkedInExportPreview(
            ok=False,
            errors=(f"Archive contains more than {MAX_ZIP_ENTRIES} entries.",),
        )

    total_uncompressed = 0
    approved_paths: dict[str, str] = {}
    for info in entries:
        path = info.filename
        if not _is_safe_zip_path(path):
            zf.close()
            return LinkedInExportPreview(
                ok=False,
                errors=(f"Unsafe archive path rejected: {path!r}",),
            )
        if info.is_dir():
            continue
        if _is_nested_archive(path):
            zf.close()
            return LinkedInExportPreview(
                ok=False,
                errors=(f"Nested archives are not allowed: {path!r}",),
            )
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            zf.close()
            return LinkedInExportPreview(
                ok=False,
                errors=(
                    f"Uncompressed contents exceed {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MiB.",
                ),
            )
        base = _normalize_basename(path)
        if base in APPROVED_BASENAMES:
            if base in approved_paths:
                zf.close()
                return LinkedInExportPreview(
                    ok=False,
                    errors=(
                        f"Duplicate approved file {base!r} found at multiple paths: "
                        f"{approved_paths[base]!r} and {path!r}",
                    ),
                )
            approved_paths[base] = path
        elif _looks_like_ignored_export_file(path):
            ignored.append(path)
        else:
            ignored.append(path)

    counts: dict[str, int] = {
        "connections": 0,
        "message_threads": 0,
        "messages": 0,
        "invitations": 0,
        "company_follows": 0,
    }
    duplicate_urls: list[str] = []

    for basename in sorted(approved_paths):
        path = approved_paths[basename]
        try:
            raw = zf.read(path)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            errors.append(f"Could not read {path!r}: {exc}")
            continue
        if len(raw) > MAX_UNCOMPRESSED_BYTES:
            errors.append(f"{basename}: uncompressed file too large")
            continue
        try:
            rows, row_warnings = _read_csv_rows(raw, basename=basename)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        file_warnings = list(row_warnings)
        valid_rows = 0
        skipped_rows = 0
        if basename == "connections.csv":
            valid_rows, skipped_rows, dups, parse_warnings = _parse_connections(rows)
            counts["connections"] = valid_rows
            duplicate_urls.extend(dups)
            file_warnings.extend(parse_warnings)
        elif basename == "messages.csv":
            threads, valid_rows, parse_warnings = _parse_messages(rows)
            counts["message_threads"] = threads
            counts["messages"] = valid_rows
            skipped_rows = len(rows) - valid_rows
            file_warnings.extend(parse_warnings)
        elif basename == "invitations.csv":
            valid_rows, skipped_rows, parse_warnings = _parse_invitations(rows)
            counts["invitations"] = valid_rows
            file_warnings.extend(parse_warnings)
        elif basename == "company follows.csv":
            valid_rows, skipped_rows, parse_warnings = _parse_company_follows(rows)
            counts["company_follows"] = valid_rows
            file_warnings.extend(parse_warnings)
        summaries.append(
            ParsedFileSummary(
                basename=basename,
                archive_path=path,
                row_count=len(rows),
                valid_rows=valid_rows,
                skipped_rows=skipped_rows,
                warnings=tuple(file_warnings),
            )
        )
        warnings.extend(file_warnings)

    zf.close()

    if errors:
        return LinkedInExportPreview(
            ok=False,
            errors=tuple(errors),
            warnings=tuple(warnings),
            ignored_files=tuple(sorted(set(ignored))),
            files=tuple(summaries),
        )

    proposed = {
        "new_connections": counts["connections"],
        "message_threads": counts["message_threads"],
        "invitations": counts["invitations"],
        "company_follows": counts["company_follows"],
    }

    if not summaries:
        warnings = list(warnings)
        warnings.append("No approved CSV files found in archive.")

    return LinkedInExportPreview(
        ok=True,
        warnings=tuple(warnings),
        ignored_files=tuple(sorted(set(ignored))),
        files=tuple(summaries),
        connection_count=counts["connections"],
        message_thread_count=counts["message_threads"],
        message_row_count=counts["messages"],
        invitation_count=counts["invitations"],
        company_follow_count=counts["company_follows"],
        duplicate_profile_urls=tuple(sorted(set(duplicate_urls))),
        proposed_changes=proposed,
    )


def export_limits_for_client() -> dict[str, Any]:
    """JSON-serializable limits embedded in the admin imports page."""
    return {
        "maxCompressedBytes": MAX_COMPRESSED_BYTES,
        "maxUncompressedBytes": MAX_UNCOMPRESSED_BYTES,
        "maxZipEntries": MAX_ZIP_ENTRIES,
        "maxCsvRows": MAX_CSV_ROWS,
        "maxFieldLength": MAX_FIELD_LENGTH,
        "maxPathLength": MAX_PATH_LENGTH,
        "maxPreambleScanLines": MAX_PREAMBLE_SCAN_LINES,
        "approvedBasenames": sorted(APPROVED_BASENAMES),
    }
