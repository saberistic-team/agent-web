"""Tests for LinkedIn export ZIP parsing (Python canonical spec)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.linkedin_export_parser import (
    MAX_COMPRESSED_BYTES,
    MAX_CSV_ROWS,
    MAX_FIELD_LENGTH,
    MAX_PATH_LENGTH,
    MAX_UNCOMPRESSED_BYTES,
    MAX_ZIP_ENTRIES,
    export_limits_for_client,
    parse_linkedin_export_zip,
)


def _build_export_zip(
    files: dict[str, str | bytes],
    *,
    prefix: str = "LinkedIn Export/",
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            path = f"{prefix}{name}" if prefix else name
            payload = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(path, payload, compress_type=compression)
    return buf.getvalue()


CONNECTIONS_CSV = (
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Analytical Engines,Engineer,01 Jan 2024\n"
    "Grace,Hopper,https://linkedin.com/in/grace-hopper/,grace@example.com,"
    "US Navy,Admiral,02 Feb 2024\n"
    "Alan,Turing,https://linkedin.com/in/ada-lovelace/,alan@example.com,"
    "Bletchley,Cryptanalyst,03 Mar 2024\n"
)

CONNECTIONS_CSV_WITH_PREAMBLE = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the email '
    'addresses are missing. You will only see email addresses for connections who '
    'have allowed their connections to see or download their email address."\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Analytical Engines,Engineer,01 Jan 2024\n"
    "Grace,Hopper,https://linkedin.com/in/grace-hopper/,grace@example.com,"
    "US Navy,Admiral,02 Feb 2024\n"
    "Alan,Turing,https://linkedin.com/in/ada-lovelace/,alan@example.com,"
    "Bletchley,Cryptanalyst,03 Mar 2024\n"
)

CONNECTIONS_CSV_WITH_SINGLE_LINE_PREAMBLE = (
    "Notes:\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Analytical Engines,Engineer,01 Jan 2024\n"
)

MESSAGES_CSV = (
    "CONVERSATION ID,FROM,TO,SUBJECT,CONTENT,DATE,FOLDER\n"
    "conv-1,Ada Lovelace,Grace Hopper,Hello,Secret body text,2024-01-01,INBOX\n"
    "conv-1,Grace Hopper,Ada Lovelace,Re: Hello,More secret text,2024-01-02,INBOX\n"
    "conv-2,Alan Turing,Ada Lovelace,Ping,Private,2024-02-01,INBOX\n"
)

INVITATIONS_CSV = (
    "From,To,Sent At,Message\n"
    "Ada Lovelace,Grace Hopper,2024-01-01,Let's connect\n"
)

COMPANY_FOLLOWS_CSV = (
    "Organization,Followed On\n"
    "Northwind Labs,2024-01-01\n"
    "Helios Rail,2024-02-01\n"
)


@pytest.mark.unit
@pytest.mark.integration
def test_parse_representative_export_variant() -> None:
    data = _build_export_zip(
        {
            "Connections.csv": CONNECTIONS_CSV,
            "messages.csv": MESSAGES_CSV,
            "Invitations.csv": INVITATIONS_CSV,
            "Company Follows.csv": COMPANY_FOLLOWS_CSV,
            "Logins.csv": "Date,IP Address\n2024-01-01,127.0.0.1\n",
            "PhoneNumbers.csv": "Number\n+15551234567\n",
        }
    )
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 3
    assert result.message_thread_count == 2
    assert result.message_row_count == 3
    assert result.invitation_count == 1
    assert result.company_follow_count == 2
    assert len(result.files) == 4
    assert "Logins.csv" in "".join(result.ignored_files)
    assert "https://linkedin.com/in/ada-lovelace" in result.duplicate_profile_urls
    assert result.proposed_changes["new_connections"] == 3


@pytest.mark.unit
@pytest.mark.integration
def test_parse_deflated_export_variant() -> None:
    data = _build_export_zip(
        {"Connections.csv": CONNECTIONS_CSV},
        compression=zipfile.ZIP_DEFLATED,
    )
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 3


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_invalid_zip() -> None:
    result = parse_linkedin_export_zip(b"not-a-zip")
    assert result.ok is False
    assert any("valid ZIP" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_path_traversal() -> None:
    data = _build_export_zip({"../Connections.csv": CONNECTIONS_CSV}, prefix="")
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("Unsafe archive path" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_nested_archive() -> None:
    data = _build_export_zip({"payload.zip": b"PK\x03\x04"})
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("Nested archives" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_zip_bomb_uncompressed_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.linkedin_export_parser.MAX_UNCOMPRESSED_BYTES", 1024)
    data = _build_export_zip({"Connections.csv": "x" * 2048})
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("Uncompressed contents exceed" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_compressed_size_limit() -> None:
    result = parse_linkedin_export_zip(b"x" * (MAX_COMPRESSED_BYTES + 1))
    assert result.ok is False
    assert any("compressed limit" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_too_many_entries() -> None:
    files = {f"ignored-{i}.txt": "x" for i in range(MAX_ZIP_ENTRIES + 1)}
    data = _build_export_zip(files)
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("more than" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_malformed_csv() -> None:
    data = _build_export_zip({"Connections.csv": "\x00\x01binary"})
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("not valid CSV" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_warns_on_unexpected_schema() -> None:
    data = _build_export_zip({"Connections.csv": "Name,Company\nAda,ACME\n"})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert any("unexpected schema" in w.lower() for w in result.warnings)


@pytest.mark.unit
@pytest.mark.integration
def test_no_approved_files_warning() -> None:
    data = _build_export_zip({"Logins.csv": "Date\n2024\n", "Profile.csv": "Name\nAda\n"})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert any("No approved CSV files" in w for w in result.warnings)
    assert result.files == ()


@pytest.mark.unit
@pytest.mark.integration
def test_messages_never_surface_content_in_preview() -> None:
    data = _build_export_zip({"messages.csv": MESSAGES_CSV})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    dumped = repr(result)
    assert "Secret body text" not in dumped
    assert "More secret text" not in dumped


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_absolute_zip_path() -> None:
    data = _build_export_zip({"Connections.csv": CONNECTIONS_CSV}, prefix="/")
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("Unsafe archive path" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_windows_drive_path() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("C:/Connections.csv", CONNECTIONS_CSV)
    result = parse_linkedin_export_zip(buf.getvalue())
    assert result.ok is False
    assert any("Unsafe archive path" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_truncates_overlong_csv_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.linkedin_export_parser.MAX_FIELD_LENGTH", 8)
    long_name = "A" * 20
    csv_text = (
        "First Name,Last Name,URL\n"
        f"{long_name},Lovelace,https://linkedin.com/in/ada-lovelace/\n"
    )
    data = _build_export_zip({"Connections.csv": csv_text})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert any("exceeds max length" in w for w in result.warnings)


@pytest.mark.unit
@pytest.mark.integration
def test_skips_invalid_profile_urls() -> None:
    csv_text = (
        "First Name,Last Name,URL\n"
        "Bad,Row,://missing-host\n"
        "Good,Row,https://linkedin.com/in/good-row/\n"
    )
    data = _build_export_zip({"Connections.csv": csv_text})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 1


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_duplicate_approved_file() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/Connections.csv", CONNECTIONS_CSV)
        zf.writestr("b/Connections.csv", CONNECTIONS_CSV)
    result = parse_linkedin_export_zip(buf.getvalue())
    assert result.ok is False
    assert any("Duplicate approved file" in err for err in result.errors)
    assert any("a/Connections.csv" in err and "b/Connections.csv" in err for err in result.errors)


@pytest.mark.unit
@pytest.mark.integration
def test_truncates_csv_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.linkedin_export_parser.MAX_CSV_ROWS", 2)
    rows = "First Name,Last Name,URL\n" + "".join(
        f"Person{i},Test,https://linkedin.com/in/person{i}/\n" for i in range(5)
    )
    data = _build_export_zip({"Connections.csv": rows})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert any("truncated at" in w for w in result.warnings)


@pytest.mark.unit
@pytest.mark.integration
def test_export_limits_for_client_matches_parser_constants() -> None:
    limits = export_limits_for_client()
    assert limits["maxCompressedBytes"] == MAX_COMPRESSED_BYTES
    assert limits["maxFieldLength"] == MAX_FIELD_LENGTH
    assert "connections.csv" in limits["approvedBasenames"]


@pytest.mark.unit
@pytest.mark.integration
def test_parse_connections_csv_with_notes_preamble() -> None:
    data = _build_export_zip({"Connections.csv": CONNECTIONS_CSV_WITH_PREAMBLE})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 3
    assert not any("unexpected schema" in w.lower() for w in result.warnings)
    assert not any("no rows with a recognizable profile url" in w.lower() for w in result.warnings)
    conn_file = next(f for f in result.files if f.basename == "connections.csv")
    assert conn_file.valid_rows == 3
    assert conn_file.row_count == 3


@pytest.mark.unit
@pytest.mark.integration
def test_parse_connections_csv_with_single_line_preamble() -> None:
    data = _build_export_zip({"Connections.csv": CONNECTIONS_CSV_WITH_SINGLE_LINE_PREAMBLE})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 1
    assert not any("unexpected schema" in w.lower() for w in result.warnings)


@pytest.mark.unit
@pytest.mark.integration
def test_parse_connections_csv_without_preamble_unchanged() -> None:
    data = _build_export_zip({"Connections.csv": CONNECTIONS_CSV})
    result = parse_linkedin_export_zip(data)
    assert result.ok is True
    assert result.connection_count == 3


@pytest.mark.unit
@pytest.mark.integration
def test_rejects_headerless_csv_after_preamble_scan() -> None:
    preamble = "\n".join(f"line-{i}" for i in range(25)) + "\n"
    data = _build_export_zip({"Connections.csv": preamble})
    result = parse_linkedin_export_zip(data)
    assert result.ok is False
    assert any("missing CSV header row" in err for err in result.errors)
