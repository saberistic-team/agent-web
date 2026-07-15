"""Unit tests for LinkedIn export ZIP parsing."""

from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.integration]

from app.linkedin_export import (
    MAX_COMPRESSED_BYTES,
    MAX_CSV_ROWS,
    MAX_FIELD_LENGTH,
    MAX_UNCOMPRESSED_BYTES,
    build_zip_bytes,
    export_limits,
    parse_linkedin_export_zip,
)

CONNECTIONS_CSV = """First Name,Last Name,URL,Email Address,Company,Position,Connected On
Alex,Nguyen,https://www.linkedin.com/in/alex-nguyen,alex@northwind.io,Northwind Labs,VP Engineering,01 Mar 2024
Sam,Patel,https://linkedin.com/in/sam-patel,,Helios Rail,Founder,12 Jan 2023
"""

MESSAGES_CSV = """CONVERSATION ID,CONVERSATION TITLE,FROM,TO,DATE,SUBJECT,CONTENT,FOLDER
conv-1,Intro thread,Alex Nguyen,Sam Patel,2024-03-01T10:00:00Z,Hello,Secret message body,INBOX
conv-1,Intro thread,Sam Patel,Alex Nguyen,2024-03-01T11:00:00Z,Re: Hello,Another secret,INBOX
conv-2,Follow up,Alex Nguyen,Jordan Lee,2024-04-02T09:00:00Z,Ping,Private text,INBOX
"""

INVITATIONS_CSV = """From,To,Sent At,Message,Direction
Alex Nguyen,Jordan Lee,2024-01-08T12:00:00Z,Would love to connect,OUTGOING
"""

COMPANY_FOLLOWS_CSV = """Organization,Followed On
Northwind Labs,2024-06-01
Helios Rail,2024-05-15
"""


def _valid_export_files(*, include_messages: bool = True) -> dict[str, str]:
    files = {
        "Connections.csv": CONNECTIONS_CSV,
        "Invitations.csv": INVITATIONS_CSV,
        "Company Follows.csv": COMPANY_FOLLOWS_CSV,
        "Logins.csv": "timestamp,ip\n2024-01-01,1.2.3.4\n",
        "PhoneNumbers.csv": "number\n+15551234567\n",
    }
    if include_messages:
        files["messages.csv"] = MESSAGES_CSV
    return files


@pytest.mark.unit
def test_parse_valid_linkedin_export() -> None:
    data = build_zip_bytes(_valid_export_files())
    preview = parse_linkedin_export_zip(data)
    assert preview.ok is True
    assert preview.counts["connections"] == 2
    assert preview.counts["messages"] == 3
    assert preview.counts["conversations"] == 2
    assert preview.counts["invitations"] == 1
    assert preview.counts["company_follows"] == 2
    assert preview.ignored_file_count == 2
    assert preview.messages_redacted is True
    assert any(row["kind"] == "contact" for row in preview.proposed_changes)
    assert not any("content" in row for row in preview.proposed_changes)


@pytest.mark.unit
def test_parse_export_in_subdirectory() -> None:
    files = {
        "LinkedIn Export/Connections.csv": CONNECTIONS_CSV,
        "LinkedIn Export/Invitations.csv": INVITATIONS_CSV,
        "LinkedIn Export/Company Follows.csv": COMPANY_FOLLOWS_CSV,
    }
    preview = parse_linkedin_export_zip(build_zip_bytes(files))
    assert preview.ok is True
    assert preview.counts["connections"] == 2


@pytest.mark.unit
def test_rejects_non_zip_input() -> None:
    preview = parse_linkedin_export_zip(b"not a zip file")
    assert preview.ok is False
    assert any("valid ZIP" in err for err in preview.errors)


@pytest.mark.unit
def test_rejects_path_traversal() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../Connections.csv", CONNECTIONS_CSV)
    preview = parse_linkedin_export_zip(buffer.getvalue())
    assert preview.ok is False
    assert any("traversal" in err.lower() for err in preview.errors)


@pytest.mark.unit
def test_rejects_nested_archive() -> None:
    files = {
        "inner.zip": b"PK\x03\x04",
        "Connections.csv": CONNECTIONS_CSV,
    }
    preview = parse_linkedin_export_zip(build_zip_bytes(files))
    assert preview.ok is False
    assert any("Nested archives" in err for err in preview.errors)


@pytest.mark.unit
def test_rejects_zip_bomb_uncompressed_limit() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Connections.csv",
            "First Name,Last Name,URL\n",
            compress_type=zipfile.ZIP_STORED,
        )
        info = archive.getinfo("Connections.csv")
        info.file_size = MAX_UNCOMPRESSED_BYTES + 1
    preview = parse_linkedin_export_zip(buffer.getvalue())
    assert preview.ok is False
    assert any("Uncompressed archive exceeds" in err for err in preview.errors)


@pytest.mark.unit
def test_rejects_compressed_size_limit() -> None:
    preview = parse_linkedin_export_zip(b"x" * (MAX_COMPRESSED_BYTES + 1))
    assert preview.ok is False
    assert any("Compressed archive exceeds" in err for err in preview.errors)


@pytest.mark.unit
def test_rejects_too_many_entries() -> None:
    files = {f"ignored-{index}.txt": "x" for index in range(501)}
    files["Connections.csv"] = CONNECTIONS_CSV
    preview = parse_linkedin_export_zip(build_zip_bytes(files))
    assert preview.ok is False
    assert any("more than" in err for err in preview.errors)


@pytest.mark.unit
def test_rejects_missing_required_columns() -> None:
    bad_connections = "Email Address,Company\nalex@x.io,Northwind\n"
    files = {
        "Connections.csv": bad_connections,
        "Invitations.csv": INVITATIONS_CSV,
        "Company Follows.csv": COMPANY_FOLLOWS_CSV,
    }
    preview = parse_linkedin_export_zip(build_zip_bytes(files))
    assert preview.ok is False
    assert any("Missing required columns" in err for err in preview.errors)


@pytest.mark.unit
def test_malformed_csv_rows_skipped() -> None:
    csv_text = (
        "First Name,Last Name,URL\n"
        "Alex,Nguyen,https://linkedin.com/in/alex\n"
        "broken row\n"
        ",,\n"
    )
    preview = parse_linkedin_export_zip(
        build_zip_bytes({"Connections.csv": csv_text, "Invitations.csv": INVITATIONS_CSV, "Company Follows.csv": COMPANY_FOLLOWS_CSV})
    )
    assert preview.ok is True
    assert preview.counts["connections"] == 1
    recognized = next(row for row in preview.recognized_files if row.kind == "connections")
    assert recognized.skipped_rows >= 1


@pytest.mark.unit
def test_duplicate_connection_urls_reported() -> None:
    csv_text = (
        "First Name,Last Name,URL\n"
        "Alex,Nguyen,https://linkedin.com/in/duplicate\n"
        "Sam,Patel,https://www.linkedin.com/in/duplicate\n"
    )
    preview = parse_linkedin_export_zip(
        build_zip_bytes({"Connections.csv": csv_text, "Invitations.csv": INVITATIONS_CSV, "Company Follows.csv": COMPANY_FOLLOWS_CSV})
    )
    assert preview.ok is True
    assert any(dup["kind"] == "connection_profile_url" for dup in preview.duplicates)


@pytest.mark.unit
def test_no_recognized_files_error() -> None:
    preview = parse_linkedin_export_zip(
        build_zip_bytes({"Logins.csv": "x\n", "Ads.csv": "y\n"})
    )
    assert preview.ok is False
    assert any("No recognized LinkedIn export files" in err for err in preview.errors)


@pytest.mark.unit
def test_field_length_truncation_warning() -> None:
    long_value = "x" * (MAX_FIELD_LENGTH + 50)
    csv_text = f"First Name,Last Name,URL,Company\nAlex,Nguyen,https://linkedin.com/in/alex,{long_value}\n"
    preview = parse_linkedin_export_zip(
        build_zip_bytes({"Connections.csv": csv_text, "Invitations.csv": INVITATIONS_CSV, "Company Follows.csv": COMPANY_FOLLOWS_CSV})
    )
    assert preview.ok is True
    assert any("truncated" in warning.lower() for warning in preview.warnings)


@pytest.mark.unit
def test_row_limit_warning() -> None:
    header = "First Name,Last Name,URL\n"
    rows = "".join(f"Person{index},Test,https://linkedin.com/in/p{index}\n" for index in range(MAX_CSV_ROWS + 5))
    preview = parse_linkedin_export_zip(
        build_zip_bytes({"Connections.csv": header + rows, "Invitations.csv": INVITATIONS_CSV, "Company Follows.csv": COMPANY_FOLLOWS_CSV})
    )
    assert preview.ok is True
    assert any("Row limit" in warning for warning in preview.warnings)


@pytest.mark.unit
def test_export_limits_match_browser_script() -> None:
    js_path = __import__("pathlib").Path(__file__).resolve().parents[1] / "site/assets/linkedin-export.js"
    js_text = js_path.read_text(encoding="utf-8")
    limits = export_limits()
    assert "MAX_COMPRESSED_BYTES: 50 * 1024 * 1024" in js_text
    assert "MAX_UNCOMPRESSED_BYTES: 200 * 1024 * 1024" in js_text
    assert f"MAX_ARCHIVE_ENTRIES: {limits['MAX_ARCHIVE_ENTRIES']}" in js_text
    assert f"MAX_CSV_ROWS: {limits['MAX_CSV_ROWS']}" in js_text
    assert f"MAX_FIELD_LENGTH: {limits['MAX_FIELD_LENGTH']}" in js_text
    for suffix in limits["NESTED_ARCHIVE_SUFFIXES"]:
        assert suffix in js_text


@pytest.mark.unit
def test_preview_to_dict_serializable() -> None:
    preview = parse_linkedin_export_zip(build_zip_bytes(_valid_export_files()))
    payload = preview.to_dict()
    assert payload["ok"] is True
    assert payload["counts"]["connections"] == 2
