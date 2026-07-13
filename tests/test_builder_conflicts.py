#!/usr/bin/env python3
"""Unit tests for Builder conflict-resolution helpers."""

from __future__ import annotations

from builder_conflicts import (
    default_resolve_file,
    format_conflict_resolution_brief,
    format_merge_conflict_hard_fail,
    pr_needs_conflict_resolution,
    strip_conflict_markers_prefer_head,
    summarize_recent_closed_work,
)


def test_pr_needs_conflict_resolution_dirty() -> None:
    assert pr_needs_conflict_resolution(
        {"mergeable": False, "mergeable_state": "dirty", "state": "open"}
    )
    assert pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "dirty", "state": "open"}
    )
    assert not pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "clean", "state": "open"}
    )
    assert not pr_needs_conflict_resolution(
        {"mergeable": True, "mergeable_state": "clean", "merged": True}
    )


def test_strip_conflict_markers_prefer_head() -> None:
    text = (
        "line1\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> main\n"
        "line2\n"
    )
    assert strip_conflict_markers_prefer_head(text) == "line1\nours\nline2\n"


def test_default_resolve_file_uses_chat_when_clean(monkeypatch) -> None:
    conflicted = (
        "<<<<<<< HEAD\nfeature\n=======\nseo\n>>>>>>> main\n"
    )

    def chat(system: str, user: str) -> str:
        assert "Conflicted file" in user
        return "feature\nseo\n"

    assert default_resolve_file("site/index.html", conflicted, "brief", chat=chat) == (
        "feature\nseo"
    )


def test_default_resolve_file_falls_back_when_chat_returns_markers() -> None:
    conflicted = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n"

    def chat(system: str, user: str) -> str:
        return conflicted

    assert default_resolve_file("app/main.py", conflicted, "brief", chat=chat) == "ours\n"


def test_summarize_recent_closed_work(monkeypatch) -> None:
    monkeypatch.setattr(
        "builder_conflicts.list_recent_merged_prs",
        lambda repo, limit=8: [
            {
                "number": 73,
                "title": "Repair technical SEO (#68)",
                "body": "Closes #68\n\nCanonical + robots.",
                "merged_at": "2026-07-13T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        "builder_conflicts.pr_changed_paths",
        lambda repo, pr: ["app/seo.py", "site/index.html"],
    )
    monkeypatch.setattr(
        "builder_conflicts.list_recent_closed_issues",
        lambda repo, limit=8: [
            {"number": 68, "title": "Repair technical SEO", "body": "## Goal\nFix SEO."}
        ],
    )
    text = summarize_recent_closed_work("saberistic-team/agent-web")
    assert "#73" in text
    assert "`app/seo.py`" in text
    assert "#68" in text
    assert "Recently closed issues" in text


def test_format_conflict_resolution_brief(monkeypatch) -> None:
    monkeypatch.setattr(
        "builder_conflicts.summarize_recent_closed_work",
        lambda repo, **kwargs: "## Recently merged PRs\n- #73: SEO",
    )
    pr = {
        "number": 76,
        "title": "builder: OG (#67)",
        "body": "Closes #67",
        "head": {"ref": "builder/67-p1"},
        "base": {"ref": "main"},
    }
    brief = format_conflict_resolution_brief(
        "saberistic-team/agent-web",
        67,
        pr,
        conflicted_paths=["site/index.html"],
    )
    assert "issue #67" in brief
    assert "PR #76" in brief
    assert "`site/index.html`" in brief
    assert "#73" in brief


def test_maybe_resolve_pr_conflicts_skips_clean(monkeypatch) -> None:
    from builder_conflicts import maybe_resolve_pr_conflicts

    monkeypatch.setattr(
        "builder_conflicts.linked_open_prs",
        lambda repo, issue: [{"number": 76, "title": "x (#67)", "body": "Closes #67"}],
    )
    monkeypatch.setattr(
        "builder_conflicts.refresh_pr",
        lambda repo, n: {
            "number": 76,
            "mergeable": True,
            "mergeable_state": "clean",
            "state": "open",
            "head": {"ref": "builder/67-p1"},
            "base": {"ref": "main"},
        },
    )
    result = maybe_resolve_pr_conflicts("saberistic-team/agent-web", 67)
    assert result["status"] == "clean"
    assert result["pr"] == 76


def test_linked_pr_conflict_status_dirty(monkeypatch) -> None:
    from builder_conflicts import linked_pr_conflict_status

    monkeypatch.setattr(
        "builder_conflicts.linked_open_prs",
        lambda repo, issue: [{"number": 80, "title": "x (#70)", "body": "Closes #70"}],
    )
    monkeypatch.setattr(
        "builder_conflicts.refresh_pr",
        lambda repo, n: {
            "number": 80,
            "mergeable": False,
            "mergeable_state": "dirty",
            "state": "open",
            "head": {"ref": "builder/70-x"},
        },
    )
    status = linked_pr_conflict_status("saberistic-team/agent-web", 70)
    assert status["status"] == "dirty"
    assert status["pr"] == 80
    assert "return to Builder" in format_merge_conflict_hard_fail(status)


def test_merge_fetch_uses_explicit_refspec_for_single_branch_clone(monkeypatch) -> None:
    """--single-branch clones need refspec fetch so origin/main is mergeable."""
    from pathlib import Path

    from builder_conflicts import merge_default_into_pr_branch

    git_calls: list[list[str]] = []

    def fake_run_git(args: list[str], *, cwd: Path, check: bool = True):
        git_calls.append(list(args))
        if args[:2] == ["merge", "origin/main"]:
            class _Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Proc()
        if args[:2] == ["push", "origin"]:
            class _Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Proc()
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("builder_conflicts._run_git", fake_run_git)
    monkeypatch.setattr(
        "builder_conflicts.summarize_recent_closed_work",
        lambda repo, **kwargs: "",
    )

    work_dir = Path("/tmp/builder-conflict-test-repo")
    pr = {
        "number": 74,
        "title": "builder: insights (#69)",
        "body": "Closes #69",
        "head": {"ref": "builder/69-p1-build-an-authority-content-system-for"},
        "base": {"ref": "main"},
    }
    merge_default_into_pr_branch(
        "saberistic-team/agent-web",
        pr,
        work_dir=work_dir,
        push=False,
    )

    fetch_calls = [c for c in git_calls if c and c[0] == "fetch"]
    assert fetch_calls, "expected a git fetch before merge"
    fetch = fetch_calls[0]
    assert fetch[1] == "origin"
    assert fetch[2] == "+refs/heads/main:refs/remotes/origin/main"


def test_linked_pr_conflict_status_clean(monkeypatch) -> None:
    from builder_conflicts import linked_pr_conflict_status

    monkeypatch.setattr(
        "builder_conflicts.linked_open_prs",
        lambda repo, issue: [{"number": 81, "title": "x (#71)", "body": "Closes #71"}],
    )
    monkeypatch.setattr(
        "builder_conflicts.refresh_pr",
        lambda repo, n: {
            "number": 81,
            "mergeable": True,
            "mergeable_state": "clean",
            "state": "open",
            "head": {"ref": "builder/71-x"},
        },
    )
    status = linked_pr_conflict_status("saberistic-team/agent-web", 71)
    assert status["status"] == "clean"
