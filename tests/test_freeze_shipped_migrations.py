"""Unit tests for post-deploy migration digest freezing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from freeze_shipped_migrations import (
    apply_freeze_to_definitions,
    build_freeze_files_at,
    commit_message,
    format_frozen_block,
    freeze_branch_name,
    freeze_pr_body,
    load_definitions,
    maybe_commit_freeze,
    missing_frozen_digests,
)


@pytest.mark.unit
def test_missing_reports_only_unfrozen_versions() -> None:
    missing = missing_frozen_digests()
    frozen = load_definitions().FROZEN_MIGRATION_DIGESTS
    for version, digest in missing.items():
        assert version not in frozen
        assert len(digest) == 64


@pytest.mark.unit
def test_format_and_apply_freeze_round_trip(tmp_path: Path) -> None:
    definitions = tmp_path / "app" / "migrations"
    definitions.mkdir(parents=True)
    source = '''"""Ordered migrations."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass

@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_sql: str

def migration_content_digest(migration: Migration) -> str:
    payload = f"{migration.version}\\0{migration.name}\\0{migration.up_sql}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# When adding a new migration, leave prior entries unchanged and freeze the
# new version only after it has shipped to production.
FROZEN_MIGRATION_DIGESTS: dict[str, str] = {
    "001": "aaa",
}

MIGRATIONS: tuple[Migration, ...] = (
    Migration(version="001", name="one", up_sql="CREATE 1"),
    Migration(version="002", name="two", up_sql="CREATE 2"),
)
'''
    path = definitions / "definitions.py"
    path.write_text(source, encoding="utf-8")

    missing = missing_frozen_digests(tmp_path)
    assert list(missing) == ["002"]
    files = build_freeze_files_at(tmp_path)
    assert len(files) == 1
    rel, content = files[0]
    assert rel == "app/migrations/definitions.py"
    text = content.decode("utf-8")
    assert '"002"' in text
    assert "Freeze shipped migrations" in text
    assert "freeze_shipped_migrations.py" in text

    path.write_bytes(content)
    assert missing_frozen_digests(tmp_path) == {}
    assert build_freeze_files_at(tmp_path) == []


@pytest.mark.unit
def test_apply_freeze_preserves_existing_digest() -> None:
    text = (
        "FROZEN_MIGRATION_DIGESTS: dict[str, str] = {\n"
        '    "001": "keep-me",\n'
        "}\n"
    )
    updated = apply_freeze_to_definitions(
        text,
        {"001": "keep-me", "002": "new-digest"},
    )
    assert '"001": "keep-me"' in updated
    assert '"002": "new-digest"' in updated
    assert format_frozen_block({"001": "a"}) == '    "001": "a",'


@pytest.mark.unit
def test_freeze_branch_name_is_deterministic() -> None:
    assert freeze_branch_name(["019", "020"]) == "deploy/freeze-019-020"


@pytest.mark.unit
def test_freeze_pr_body_mentions_versions_and_auto_merge() -> None:
    body = freeze_pr_body(["019"])
    assert "019" in body
    assert "auto-merge" in body.lower()
    assert "WORKFLOW_GOVERNANCE" in body


@pytest.mark.unit
def test_maybe_commit_freeze_noop_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "freeze_shipped_migrations.missing_frozen_digests",
        lambda root=None: {},
    )
    with patch("github_api.put_files") as put:
        result = maybe_commit_freeze("o/r", "main")
    assert result == {"frozen": [], "sha": None}
    put.assert_not_called()


def _write_definitions_with_pending_migration(tmp_path: Path) -> tuple[str, str]:
    import hashlib

    definitions = tmp_path / "app" / "migrations"
    definitions.mkdir(parents=True)
    digest_001 = hashlib.sha256(b"001\0one\0A").hexdigest()
    digest_002 = hashlib.sha256(b"002\0two\0B").hexdigest()
    (definitions / "definitions.py").write_text(
        f'''from __future__ import annotations
import hashlib
from dataclasses import dataclass

@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up_sql: str

def migration_content_digest(migration: Migration) -> str:
    payload = f"{{migration.version}}\\0{{migration.name}}\\0{{migration.up_sql}}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# When adding a new migration, leave prior entries unchanged and freeze the
# new version only after it has shipped to production.
FROZEN_MIGRATION_DIGESTS: dict[str, str] = {{
    "001": "{digest_001}",
}}

MIGRATIONS = (
    Migration("001", "one", "A"),
    Migration("002", "two", "B"),
)
''',
        encoding="utf-8",
    )
    return digest_001, digest_002


@pytest.mark.unit
def test_maybe_commit_freeze_opens_pr_with_auto_merge_when_missing(
    tmp_path: Path,
) -> None:
    _digest_001, digest_002 = _write_definitions_with_pending_migration(tmp_path)

    with (
        patch("github_api.find_open_pr_for_branch", return_value=None) as find_pr,
        patch("github_api.create_branch") as create_branch,
        patch("github_api.put_files", return_value="newsha") as put,
        patch(
            "github_api.open_pull_request",
            return_value={"number": 7, "node_id": "PR_kw7", "html_url": "https://x/7"},
        ) as open_pr,
        patch("github_api.enable_auto_merge") as auto_merge,
    ):
        result = maybe_commit_freeze("o/r", "main", root=tmp_path)

    assert result["frozen"] == ["002"]
    assert result["sha"] == "newsha"
    assert result["message"] == commit_message(["002"])
    assert result["pr_number"] == 7
    assert result["pr_url"] == "https://x/7"

    find_pr.assert_called_once_with("o/r", "deploy/freeze-002")
    create_branch.assert_called_once_with("o/r", "deploy/freeze-002", base_branch="main")

    put.assert_called_once()
    put_args = put.call_args.args
    assert put_args[0] == "o/r"
    assert put_args[1] == "deploy/freeze-002"
    assert put_args[3] == commit_message(["002"])
    written = put_args[2][0][1].decode("utf-8")
    assert digest_002 in written

    open_pr.assert_called_once_with(
        "o/r",
        head="deploy/freeze-002",
        base="main",
        title=commit_message(["002"]),
        body=freeze_pr_body(["002"]),
    )
    auto_merge.assert_called_once_with("o/r", "PR_kw7")


@pytest.mark.unit
def test_maybe_commit_freeze_reuses_existing_open_pr(tmp_path: Path) -> None:
    _write_definitions_with_pending_migration(tmp_path)

    with (
        patch(
            "github_api.find_open_pr_for_branch",
            return_value={"number": 9, "html_url": "https://x/9"},
        ),
        patch("github_api.create_branch") as create_branch,
        patch("github_api.put_files") as put,
        patch("github_api.open_pull_request") as open_pr,
        patch("github_api.enable_auto_merge") as auto_merge,
    ):
        result = maybe_commit_freeze("o/r", "main", root=tmp_path)

    assert result["frozen"] == ["002"]
    assert result["sha"] is None
    assert result["pr_number"] == 9
    assert result["pr_url"] == "https://x/9"
    create_branch.assert_not_called()
    put.assert_not_called()
    open_pr.assert_not_called()
    auto_merge.assert_not_called()
