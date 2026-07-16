"""Browser-executed regression coverage for ``site/assets/linkedin-import.js``.

The production failure this covers (#224) lives in the *deployed JavaScript*
ZIP parser, not the Python canonical spec (``app/linkedin_export_parser.py``,
covered separately by ``tests/test_linkedin_export_parser.py``). These tests
drive a real Chromium browser (Playwright) against the actual authenticated
``/admin/imports`` page, uploading hand-built ZIP fixtures through the real
file input so the exact browser-side ``inflateEntry`` / ``parseZip`` code
path that failed in production is exercised end to end.

Isolation: marked ``browser`` (see ``pytest.ini``) and skipped when the
``playwright`` package or its Chromium browser is unavailable, mirroring how
``tests/pg_contract`` skips without ``TEST_DATABASE_URL``. CI runs this suite
in its own workflow (``.github/workflows/browser-linkedin-import.yml``) that
installs Playwright, keeping the default fast CI job dependency-free.

Auth: the real (non-preview) ``/admin/imports`` page is served by an
in-process ``uvicorn`` server with the admin-session DB lookups mocked the
same way ``tests/test_admin_imports.py`` mocks them for ``TestClient`` — this
is a genuinely authenticated session (server-validated cookie/token hash),
just without a live PostgreSQL dependency, so the suite stays fast and
hermetic.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Iterator
from unittest.mock import MagicMock, patch

import pytest

playwright_sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed; skipping browser suite"
)
sync_playwright = playwright_sync_api.sync_playwright

import uvicorn  # noqa: E402

from app import admin_auth, db  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = pytest.mark.browser

CONNECTIONS_CSV = (
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Analytical Engines,Engineer,01 Jan 2024\n"
)

CONNECTIONS_CSV_WITH_PREAMBLE = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the email '
    "addresses are missing. You will only see email addresses for connections who "
    'have allowed their connections to see or download their email address."\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Jane,Doe,https://www.linkedin.com/in/jane-doe,,Acme Corp,Engineer,10 Jul 2026\n"
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Analytical Engines,Engineer,01 Jan 2024\n"
    "Grace,Hopper,https://linkedin.com/in/grace-hopper/,grace@example.com,"
    "US Navy,Admiral,02 Feb 2024\n"
)

CONNECTIONS_CSV_SINGLE_LINE_PREAMBLE = (
    "Notes:\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Jane,Doe,https://www.linkedin.com/in/jane-doe,,Acme Corp,Engineer,10 Jul 2026\n"
)
MESSAGES_CSV = (
    "CONVERSATION ID,FROM,TO,SUBJECT,CONTENT,DATE,FOLDER\n"
    "conv-1,Ada Lovelace,Grace Hopper,Hello,Super secret message body,2024-01-01,INBOX\n"
)
INVITATIONS_CSV = "From,To,Sent At,Message\nAda Lovelace,Grace Hopper,2024-01-01,Let's connect\n"
COMPANY_FOLLOWS_CSV = "Organization,Followed On\nNorthwind Labs,2024-01-01\n"

ALL_FOUR = {
    "Connections.csv": CONNECTIONS_CSV,
    "messages.csv": MESSAGES_CSV,
    "Invitations.csv": INVITATIONS_CSV,
    "Company Follows.csv": COMPANY_FOLLOWS_CSV,
}


# --- Minimal raw ZIP writer -------------------------------------------------
#
# ``zipfile.ZipFile`` cannot produce the exact byte-for-byte malformed /
# data-descriptor / mismatched-metadata archives these tests need (it always
# back-patches accurate local-header sizes when writing to a seekable
# stream). This tiny writer gives full control over local vs. central
# directory fields so we can reproduce the real LinkedIn-export failure mode
# (general-purpose flag bit 3 with zeroed local-header sizes) and the other
# malformed-input cases the issue calls out.


def _deflate_raw(data: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    return co.compress(data) + co.flush()


@dataclass
class RawZipEntry:
    name: str
    data: bytes = b""
    is_dir: bool = False
    compression: int = 0  # 0 = stored, 8 = DEFLATE
    general_purpose_flag: int = 0
    zero_local_sizes: bool = False  # simulate bit-3 data-descriptor placeholders
    central_compression: int | None = None  # override -> method mismatch
    truncate_compressed_by: int = 0
    central_compressed_size_override: int | None = None
    local_header_offset_override: int | None = None


def build_raw_zip(entries: list[RawZipEntry]) -> bytes:
    body = bytearray()
    central = bytearray()

    for entry in entries:
        offset = len(body)
        name_bytes = entry.name.encode("utf-8")
        compressed = _deflate_raw(entry.data) if entry.compression == 8 else entry.data
        if entry.truncate_compressed_by:
            cut = max(0, len(compressed) - entry.truncate_compressed_by)
            compressed = compressed[:cut]
        crc = zlib.crc32(entry.data) & 0xFFFFFFFF
        uncompressed_size = len(entry.data)
        compressed_size = len(compressed)

        local_crc = 0 if entry.zero_local_sizes else crc
        local_compressed_size = 0 if entry.zero_local_sizes else compressed_size
        local_uncompressed_size = 0 if entry.zero_local_sizes else uncompressed_size

        body += struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            entry.general_purpose_flag,
            entry.compression,
            0,
            0,
            local_crc,
            local_compressed_size,
            local_uncompressed_size,
            len(name_bytes),
            0,
        )
        body += name_bytes
        body += compressed
        if entry.general_purpose_flag & 0x08:
            body += struct.pack("<IIII", 0x08074B50, crc, compressed_size, uncompressed_size)

        central_offset = (
            entry.local_header_offset_override
            if entry.local_header_offset_override is not None
            else offset
        )
        central_compression = (
            entry.central_compression if entry.central_compression is not None else entry.compression
        )
        central_compressed_size = (
            entry.central_compressed_size_override
            if entry.central_compressed_size_override is not None
            else compressed_size
        )
        external_attrs = 0x10 if entry.is_dir else 0
        central += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            entry.general_purpose_flag,
            central_compression,
            0,
            0,
            crc,
            central_compressed_size,
            uncompressed_size,
            len(name_bytes),
            0,
            0,
            0,
            0,
            external_attrs,
            central_offset,
        )
        central += name_bytes

    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        len(entries),
        len(entries),
        len(central),
        len(body),
        0,
    )
    return bytes(body) + bytes(central) + eocd


def _csv_entries(files: dict[str, str], *, prefix: str = "", **kwargs: Any) -> list[RawZipEntry]:
    entries = []
    if prefix:
        entries.append(RawZipEntry(name=prefix, is_dir=True))
    for name, content in files.items():
        entries.append(RawZipEntry(name=f"{prefix}{name}", data=content.encode("utf-8"), **kwargs))
    return entries


# --- In-process authenticated server (mirrors tests/test_admin_imports.py) --

TEST_USERNAME = "operator"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_SECRET = "test-session-secret-32chars-minimum"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveAdminServer:
    base_url: str
    cookies: dict[str, str]
    requests_log: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def live_admin_server(monkeypatch: pytest.MonkeyPatch) -> Generator[LiveAdminServer, None, None]:
    from argon2 import PasswordHasher

    password_hash = PasswordHasher().hash(TEST_PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("ADMIN_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    monkeypatch.delenv("ADMIN_PREVIEW_MODE", raising=False)

    raw_token = admin_auth.generate_session_token()
    csrf_raw = admin_auth.generate_csrf_value()
    token_hash = admin_auth.hash_session_token(raw_token)
    csrf_hash = admin_auth.hash_csrf_token(csrf_raw)
    session_store: dict[str, dict[str, Any]] = {
        token_hash: {
            "id": 1,
            "token_hash": token_hash,
            "admin_username": TEST_USERNAME,
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "revoked_at": None,
            "csrf_token_hash": csrf_hash,
        }
    }

    def _get_session(conn: Any, th: str) -> dict[str, Any] | None:
        return session_store.get(th)

    def _update_csrf(conn: Any, *, session_id: int, csrf_token_hash: str) -> None:
        for row in session_store.values():
            if row["id"] == session_id:
                row["csrf_token_hash"] = csrf_token_hash

    mock_conn = MagicMock()
    port = _free_port()

    with (
        patch.object(db, "get_admin_session_by_token_hash", side_effect=_get_session),
        patch.object(db, "update_admin_session_csrf", side_effect=_update_csrf),
        # The real DATABASE_URL is a placeholder (no live Postgres in this
        # suite); skip the real schema-migration connection FastAPI's
        # lifespan would otherwise open on startup, same spirit as the
        # TestClient-based admin tests never triggering real lifespan.
        patch.object(db, "init_db"),
        patch("app.db.db_connection") as db_conn,
        patch("app.admin_routes.db.db_connection", db_conn),
    ):
        db_conn.return_value.__enter__.return_value = mock_conn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "in-process admin server failed to start"

        try:
            yield LiveAdminServer(
                base_url=f"http://127.0.0.1:{port}",
                cookies={admin_auth.SESSION_COOKIE_NAME: raw_token},
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


def _authenticated_page(live_admin_server: LiveAdminServer, browser: Any) -> tuple[Any, Any]:
    context = browser.new_context()
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "domain": "127.0.0.1",
                "path": "/admin",
            }
            for name, value in live_admin_server.cookies.items()
        ]
    )
    page = context.new_page()
    return context, page


def goto_imports(page: Any, base_url: str) -> None:
    page.goto(f"{base_url}/admin/imports")
    page.wait_for_selector("#linkedin-import-form")


def upload_zip(page: Any, tmp_path: Path, zip_bytes: bytes, *, name: str = "export.zip") -> None:
    zip_path = tmp_path / name
    zip_path.write_bytes(zip_bytes)
    page.set_input_files("#linkedin-export-zip", str(zip_path))
    page.wait_for_function(
        "() => document.getElementById('linkedin-import-status').textContent.trim().length > 0"
        " && !document.getElementById('linkedin-import-status').textContent.includes('Parsing export locally')"
    )


def status_text(page: Any) -> str:
    return page.eval_on_selector("#linkedin-import-status", "el => el.textContent")


def preview_text(page: Any) -> str:
    return page.eval_on_selector("#linkedin-import-preview", "el => el.textContent")


def is_error(page: Any) -> bool:
    cls = page.eval_on_selector("#linkedin-import-status", "el => el.className")
    return "linkedin-import-status--error" in cls


# --- Tests -------------------------------------------------------------------


def test_connections_csv_notes_preamble_parses_correctly(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    """Current official LinkedIn exports prepend a Notes: disclaimer before the header."""
    zip_bytes = build_raw_zip(
        _csv_entries(
            {"Connections.csv": CONNECTIONS_CSV_WITH_PREAMBLE},
            prefix="LinkedIn Export/",
            compression=8,
        )
    )
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        preview = preview_text(page)
        assert "unexpected schema" not in preview.lower()
        assert "no rows with a recognizable profile url" not in preview.lower()
        assert "Connections3" in preview.replace(" ", "") or "Connections\n3" in preview
        # Stats block shows connection count 3
        assert page.locator(".linkedin-import-stats dd").first.inner_text() == "3"
    finally:
        context.close()


def test_connections_csv_single_line_preamble(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    zip_bytes = build_raw_zip(
        _csv_entries(
            {"Connections.csv": CONNECTIONS_CSV_SINGLE_LINE_PREAMBLE},
            compression=8,
        )
    )
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        preview = preview_text(page)
        assert "unexpected schema" not in preview.lower()
        assert page.locator(".linkedin-import-stats dd").first.inner_text() == "1"
    finally:
        context.close()


def test_connections_csv_no_preamble_regression(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    zip_bytes = build_raw_zip(_csv_entries({"Connections.csv": CONNECTIONS_CSV}, compression=8))
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        assert page.locator(".linkedin-import-stats dd").first.inner_text() == "1"
    finally:
        context.close()


def test_connections_csv_headerless_after_preamble_scan(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    csv_text = "\n".join(["Only one field"] * 25)
    zip_bytes = build_raw_zip(_csv_entries({"Connections.csv": csv_text}, compression=8))
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "missing CSV header row" in (status_text(page) + preview_text(page))
    finally:
        context.close()


def test_root_level_deflate_all_four_files(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    zip_bytes = build_raw_zip(_csv_entries(ALL_FOUR, compression=8))
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        assert "Failed to fetch" not in status_text(page)
        preview = preview_text(page)
        for basename in ("connections.csv", "messages.csv", "invitations.csv", "company follows.csv"):
            assert basename in preview.lower()
    finally:
        context.close()


def test_nested_export_folder(live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path) -> None:
    zip_bytes = build_raw_zip(_csv_entries(ALL_FOUR, prefix="LinkedIn Export/", compression=8))
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        preview = preview_text(page)
        assert "LinkedIn Export/Connections.csv" in preview
        assert "LinkedIn Export/messages.csv" in preview
    finally:
        context.close()


def test_explicit_directory_entry_is_safe(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = [RawZipEntry(name="LinkedIn Export/", is_dir=True)] + _csv_entries(
        ALL_FOUR, prefix="LinkedIn Export/", compression=8
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        assert "Unsafe archive path" not in status_text(page)
    finally:
        context.close()


def test_data_descriptor_bit3_zero_local_sizes(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    """The exact production regression: bit-3 data descriptors, zeroed local sizes."""
    entries = _csv_entries(
        ALL_FOUR,
        prefix="LinkedIn Export/",
        compression=8,
        general_purpose_flag=0x08,
        zero_local_sizes=True,
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
        assert "Failed to fetch" not in status_text(page)
        assert "Could not read" not in status_text(page)
        preview = preview_text(page)
        for basename in ("connections.csv", "messages.csv", "invitations.csv", "company follows.csv"):
            assert basename in preview.lower()
    finally:
        context.close()


def test_stored_method(live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path) -> None:
    zip_bytes = build_raw_zip(_csv_entries(ALL_FOUR, compression=0))
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert not is_error(page), status_text(page)
    finally:
        context.close()


def test_duplicate_approved_basenames_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries({"Connections.csv": CONNECTIONS_CSV}, prefix="a/", compression=8)
    entries += _csv_entries({"Connections.csv": CONNECTIONS_CSV}, prefix="b/", compression=8)
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "Duplicate approved file" in status_text(page)
    finally:
        context.close()


def test_truncated_compressed_data_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries(
        {"Connections.csv": CONNECTIONS_CSV},
        compression=8,
        central_compressed_size_override=999_999,
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "Failed to fetch" not in status_text(page)
        assert "Truncated or out-of-bounds" in status_text(page) or "Truncated or out-of-bounds" in preview_text(
            page
        )
    finally:
        context.close()


def test_out_of_bounds_local_header_offset_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries(
        {"Connections.csv": CONNECTIONS_CSV},
        compression=8,
        local_header_offset_override=10_000_000,
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "Invalid local file header offset" in status_text(page) or "Invalid local file header offset" in preview_text(
            page
        )
    finally:
        context.close()


def test_compression_method_mismatch_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries(
        {"Connections.csv": CONNECTIONS_CSV},
        compression=0,
        central_compression=8,
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "mismatch" in (status_text(page) + preview_text(page)).lower()
    finally:
        context.close()


def test_encrypted_entry_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries(
        {"Connections.csv": CONNECTIONS_CSV},
        compression=8,
        general_purpose_flag=0x01,
    )
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "Encrypted" in (status_text(page) + preview_text(page))
    finally:
        context.close()


def test_path_traversal_still_rejected(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    entries = _csv_entries({"Connections.csv": CONNECTIONS_CSV}, prefix="../", compression=8)
    zip_bytes = build_raw_zip(entries)
    context, page = _authenticated_page(live_admin_server, browser)
    try:
        goto_imports(page, live_admin_server.base_url)
        upload_zip(page, tmp_path, zip_bytes)
        assert is_error(page)
        assert "Unsafe archive path" in status_text(page)
    finally:
        context.close()


def test_no_network_upload_of_zip_or_message_content(
    live_admin_server: LiveAdminServer, browser: Any, tmp_path: Path
) -> None:
    """End-to-end acceptance check: full paths, counts, no upload, no fetch error."""
    zip_bytes = build_raw_zip(
        _csv_entries(
            ALL_FOUR,
            prefix="LinkedIn Export/",
            compression=8,
            general_purpose_flag=0x08,
            zero_local_sizes=True,
        )
    )
    context, page = _authenticated_page(live_admin_server, browser)
    seen_requests: list[dict[str, Any]] = []
    page.on(
        "request",
        lambda request: seen_requests.append({"method": request.method, "url": request.url}),
    )
    try:
        goto_imports(page, live_admin_server.base_url)
        del seen_requests[:]  # only observe requests made after the upload
        upload_zip(page, tmp_path, zip_bytes)

        assert not is_error(page), status_text(page)
        assert "Failed to fetch" not in status_text(page)

        preview = preview_text(page)
        for basename in ("Connections.csv", "messages.csv", "Invitations.csv", "Company Follows.csv"):
            assert f"LinkedIn Export/{basename}" in preview
        assert "Connections" in preview  # stats block renders counts

        for req in seen_requests:
            assert req["method"] not in ("POST", "PUT", "PATCH"), req
        assert not any("Super secret message body" in str(req) for req in seen_requests)
    finally:
        context.close()
