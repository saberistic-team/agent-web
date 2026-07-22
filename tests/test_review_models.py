from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

from review_models import ai_review, collect_pr_context, extract_json, reviewer_agent_cwd


def test_extract_json_recovers_truncated_approved() -> None:
    raw = (
        '{\n  "decision": "approved",\n  "meets_acceptance": true,\n'
        '  "reasons": [\n'
        '    "The dedicated About page is successfully implemented at `/about`.",\n'
        '    "The exact copy requested is fully preserved in `site/about.html`",\n'
    )
    data = extract_json(raw)
    assert data["decision"] == "approved"
    assert data["meets_acceptance"] is True


def test_extract_json_full_object() -> None:
    raw = (
        '{"decision":"changes-requested","meets_acceptance":false,'
        '"reasons":["missing tests"],"summary":"no"}'
    )
    data = extract_json(raw)
    assert data["decision"] == "changes-requested"
    assert data["meets_acceptance"] is False


def test_ai_review_approves_when_only_admin_screenshot_reasons() -> None:
    """Do not block on missing /admin PNGs or saberistic.com pre shots."""
    fake_ctx = {
        "issue_title": "Admin shell layout",
        "issue_body": "## Acceptance criteria\n\n- [ ] Desktop and mobile /admin screenshots\n",
        "pr_title": "builder: admin shell",
        "pr_body": "Closes #132",
        "commit_messages": ["builder(#132): implement admin shell"],
        "files": [{"path": "app/admin_pages.py", "patch": "+layout"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":false,'
        '"reasons":["Missing branch-admin.png and branch-admin-mobile.png",'
        '"acceptance requires desktop and mobile /admin review screenshots"],'
        '"summary":"need admin shots"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 132, 140)
    assert verdict["decision"] == "approved"
    assert verdict["meets_acceptance"] is True


def test_ai_review_approves_when_acceptance_met_despite_screenshot_nits() -> None:
    """#58: AI requested changes for .agent/screenshots + history while AC met."""
    fake_ctx = {
        "issue_title": "Brief form emails",
        "issue_body": "## Acceptance criteria\n\n- [ ] Email on submit\n",
        "pr_title": "builder: implement",
        "pr_body": "Closes #58",
        "commit_messages": ["builder(#58): implement", "review: record pre-home.png"],
        "files": [{"path": "app/main.py", "patch": "+notify"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":true,'
        '"reasons":["Adds unrelated .agent/screenshots/pr-60/pre-home.png",'
        '"Commit history should be squashed"],'
        '"summary":"AC met but nits"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 58, 60)
    assert verdict["decision"] == "approved"
    assert verdict["meets_acceptance"] is True


def test_ai_review_keeps_changes_when_real_reason_and_acceptance_claimed() -> None:
    fake_ctx = {
        "issue_title": "Brief form emails",
        "issue_body": "## Acceptance criteria\n\n- [ ] Email on submit\n",
        "pr_title": "builder: implement",
        "pr_body": "Closes #58",
        "commit_messages": ["builder(#58): implement"],
        "files": [{"path": "app/main.py", "patch": "+notify"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":true,'
        '"reasons":["Lead emails only run after Stripe succeeds; 502 skips inbox"],'
        '"summary":"real gap"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 58, 60)
    assert verdict["decision"] == "changes-requested"
    assert verdict["meets_acceptance"] is False


def test_reviewer_agent_cwd_prefers_pr_head_worktree(tmp_path) -> None:
    """#342: the AI reviewer must be rooted at the PR tree (COVERAGE_ROOT),
    never at the sibling `main` checkout — otherwise it can diff `main`
    against `pr-head/` directly and hallucinate deletions/reverts for files
    that were simply added to `main` after a stale branch forked."""
    pr_head = tmp_path / "pr-head"
    pr_head.mkdir()
    with patch.dict(os.environ, {"COVERAGE_ROOT": str(pr_head)}):
        assert reviewer_agent_cwd() == str(pr_head)


def test_reviewer_agent_cwd_falls_back_when_coverage_root_missing() -> None:
    with patch.dict(os.environ, {"COVERAGE_ROOT": "/nonexistent/pr-head-path"}):
        assert reviewer_agent_cwd() == os.getcwd()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COVERAGE_ROOT", None)
        assert reviewer_agent_cwd() == os.getcwd()


def test_collect_pr_context_paginates_beyond_default_api_page(
    monkeypatch: Any,
) -> None:
    """#377: PRs carrying >30 files (e.g. Reviewer screenshot evidence) must
    not silently drop later pages of the diff — `pulls/{pr}/files` defaults
    to a 30-item page, and an unpaginated call previously fed the AI model a
    partial (often screenshot-only) `files` list no matter how many real
    code/test/doc changes the PR actually contained."""
    screenshots = [
        {"filename": f".agent/screenshots/pr-1/branch-{i}.png", "status": "added"}
        for i in range(45)
    ]
    code_files = [
        {
            "filename": "app/admin_cache_policy.py",
            "status": "added",
            "additions": 10,
            "deletions": 0,
            "patch": "+no-store",
        }
    ]
    all_files = screenshots + code_files

    def fake_api(method: str, path: str, **_kwargs: Any) -> Any:
        assert method == "GET"
        if path.endswith("/issues/337"):
            return {"title": "issue", "body": "body"}
        if path.endswith("/pulls/1"):
            return {"title": "pr", "body": "pr body"}
        if path.endswith("/pulls/1/commits"):
            return []
        if "/pulls/1/files" in path:
            page = 1
            if "page=" in path:
                page = int(path.rsplit("page=", 1)[-1])
            start = (page - 1) * 100
            return all_files[start : start + 100]
        raise AssertionError(f"unexpected path {path}")

    # `list_pr_files` (github_api.py) resolves its own `api` call against
    # github_api's globals, not review_models's imported name, so both call
    # sites need patching to exercise the real pagination path end to end.
    monkeypatch.setattr("review_models.api", fake_api)
    monkeypatch.setattr("github_api.api", fake_api)
    ctx = collect_pr_context("o/r", 337, 1)
    filenames = [f["filename"] for f in ctx["files"]]
    assert "app/admin_cache_policy.py" in filenames
    assert ctx["screenshot_evidence_file_count"] == 45


def test_collect_pr_context_prioritizes_code_over_screenshots_in_top_20(
    monkeypatch: Any,
) -> None:
    """Even after pagination, `.agent/screenshots/...` sorts alphabetically
    before `app/`/`docs/`/`tests/`, so a plain top-20 slice of a 130-file PR
    can still be 100% screenshots. Real files must not be crowded out."""
    screenshots = [
        {"filename": f".agent/screenshots/pr-1/branch-{i}.png", "status": "added"}
        for i in range(117)
    ]
    code_files = [
        {
            "filename": name,
            "status": "added",
            "additions": 1,
            "deletions": 0,
            "patch": "+x",
        }
        for name in [
            "app/admin_cache_policy.py",
            "app/main.py",
            "docs/ADMIN_AUTH.md",
            "tests/test_admin_cache_headers.py",
        ]
    ]
    all_files = screenshots + code_files

    def fake_api(method: str, path: str, **_kwargs: Any) -> Any:
        if "/pulls/1/files" in path:
            page = 1
            if "page=" in path:
                page = int(path.rsplit("page=", 1)[-1])
            start = (page - 1) * 100
            return all_files[start : start + 100]
        if path.endswith("/pulls/1/commits"):
            return []
        return {"title": "t", "body": "b"}

    monkeypatch.setattr("review_models.api", fake_api)
    monkeypatch.setattr("github_api.api", fake_api)
    ctx = collect_pr_context("o/r", 337, 1)
    filenames = {f["filename"] for f in ctx["files"]}
    assert filenames == {
        "app/admin_cache_policy.py",
        "app/main.py",
        "docs/ADMIN_AUTH.md",
        "tests/test_admin_cache_headers.py",
    }
    assert ctx["screenshot_evidence_file_count"] == 117
