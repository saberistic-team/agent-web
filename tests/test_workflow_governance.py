from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import github_api
from validate_workflow_governance import (
    GOVERNANCE_DRIFT_ISSUE_TITLE,
    PullRequestReview,
    discover_workflow_entrypoints,
    independent_codeowner_approval_satisfied,
    load_manifest,
    path_matches_any_pattern,
    report_ruleset_drift,
    repository_files,
    ruleset_drift_errors,
    transitive_script_closure,
    validate,
    validate_ruleset_payload,
)


def _write_policy_repo(
    root: Path,
    *,
    codeowners: str = "/.github/workflows/** @human-owner\n",
    manifest_paths: list[dict[str, str]] | None = None,
    workflow_run: str = "python scripts/dispatch_queue.py\n",
    extra_scripts: dict[str, str] | None = None,
) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "dispatch.yml").write_text(
        f"name: Dispatch\njobs:\n  run:\n    steps:\n      - run: {workflow_run}"
    )
    (root / ".github" / "CODEOWNERS").write_text(codeowners)
    protected_paths = manifest_paths or [
        {
            "path": ".github/workflows/**",
            "category": "GitHub Actions workflows",
        }
    ]
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps({"schema_version": 2, "protected_paths": protected_paths})
    )
    script_name_match = re.search(r"scripts/([a-zA-Z0-9_]+)\.py", workflow_run)
    if script_name_match:
        script_name = script_name_match.group(1)
        merged_scripts = {
            f"scripts/{script_name}.py": "pass\n",
            **(extra_scripts or {}),
        }
    else:
        merged_scripts = extra_scripts or {}
    if merged_scripts:
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        for rel_path, contents in merged_scripts.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    assert validate(tmp_path) == [
        "unowned protected path: .github/workflows/dispatch.yml "
        "(from .github/workflows/**)",
        "workflow-invoked or transitive orchestration script is not protected: "
        "scripts/dispatch_queue.py",
    ]


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners="/.github/workflows/** @reviewer-bot\n"
    )

    assert validate(tmp_path) == [
        "protected path has bot CODEOWNER: .github/workflows/dispatch.yml "
        "(@reviewer-bot)",
        "workflow-invoked or transitive orchestration script is not protected: "
        "scripts/dispatch_queue.py",
    ]


def test_workflow_entrypoints_are_discovered_from_run_steps() -> None:
    discovered = discover_workflow_entrypoints(Path("."))
    assert "scripts/dispatch_queue.py" in discovered
    assert "scripts/run_agent.py" in discovered
    assert "scripts/project_sync.py" in discovered


def test_transitive_helpers_include_github_api_and_priority() -> None:
    entrypoints = discover_workflow_entrypoints(Path("."))
    closure = transitive_script_closure(entrypoints, Path("."))
    assert "scripts/github_api.py" in closure
    assert "scripts/priority.py" in closure
    assert "scripts/milestones.py" in closure


def test_unlisted_privileged_script_fails_validation(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/dispatch_queue.py @human-owner\n"
        ),
        manifest_paths=[
            {"path": ".github/workflows/**", "category": "workflows"},
            {"path": "scripts/dispatch_queue.py", "category": "dispatch"},
        ],
        extra_scripts={
            "scripts/dispatch_queue.py": "from secret_helper import mutate\n",
            "scripts/secret_helper.py": "def mutate():\n    return None\n",
        },
    )

    errors = validate(tmp_path)
    assert any(
        "workflow-invoked or transitive orchestration script is not protected: "
        "scripts/secret_helper.py" in error
        for error in errors
    )


def test_codeowners_manifest_drift_outside_inventory_fails(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/extra_owner_only.py @human-owner\n"
        ),
        extra_scripts={"scripts/extra_owner_only.py": "pass\n"},
    )

    errors = validate(tmp_path)
    assert any(
        "CODEOWNERS pattern covers a path outside the governance manifest" in error
        for error in errors
    )


def test_new_workflow_script_without_manifest_coverage_fails(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        workflow_run="python scripts/new_orchestrator.py\n",
        extra_scripts={"scripts/new_orchestrator.py": "pass\n"},
    )

    errors = validate(tmp_path)
    assert any("scripts/new_orchestrator.py" in error for error in errors)


def test_non_human_approval_is_rejected() -> None:
    ok, reason = independent_codeowner_approval_satisfied(
        pr_author="saberistic",
        head_sha="abc123",
        reviews=[
            PullRequestReview(
                author_login="saberistic-agent-web-reviewer",
                state="APPROVED",
                commit_oid="abc123",
            )
        ],
    )
    assert ok is False
    assert "automation review cannot authorize merge" in reason


def test_author_self_approval_is_rejected() -> None:
    ok, reason = independent_codeowner_approval_satisfied(
        pr_author="saberistic",
        head_sha="abc123",
        reviews=[
            PullRequestReview(
                author_login="saberistic",
                state="APPROVED",
                commit_oid="abc123",
            )
        ],
    )
    assert ok is False
    assert "author self-approval" in reason


def test_stale_approval_after_material_change_is_rejected() -> None:
    ok, reason = independent_codeowner_approval_satisfied(
        pr_author="builder-bot",
        head_sha="new-head",
        reviews=[
            PullRequestReview(
                author_login="saberistic",
                state="APPROVED",
                commit_oid="old-head",
            )
        ],
    )
    assert ok is False
    assert "stale approval" in reason


def test_independent_human_codeowner_approval_succeeds() -> None:
    ok, reason = independent_codeowner_approval_satisfied(
        pr_author="builder-bot",
        head_sha="abc123",
        reviews=[
            PullRequestReview(
                author_login="saberistic",
                state="APPROVED",
                commit_oid="abc123",
            )
        ],
    )
    assert ok is True
    assert "independent CODEOWNER approval" in reason


def test_checked_in_ruleset_template_requires_independent_review() -> None:
    payload = json.loads(
        Path(".github/rulesets/independent-workflow-review.json").read_text()
    )
    assert validate_ruleset_payload(payload) == []


@pytest.mark.parametrize(
    "mutated_field",
    [
        ("require_code_owner_review", False),
        ("require_last_push_approval", False),
        ("dismiss_stale_reviews_on_push", False),
        ("required_review_thread_resolution", False),
    ],
)
def test_ruleset_fixture_rejects_weakened_review_requirements(
    mutated_field: tuple[str, object],
) -> None:
    payload = json.loads(
        Path(".github/rulesets/independent-workflow-review.json").read_text()
    )
    field, value = mutated_field
    payload["rules"][0]["parameters"][field] = value
    errors = validate_ruleset_payload(payload)
    assert errors


def test_live_ruleset_matches_required_review_settings() -> None:
    if not Path(".github/rulesets/independent-workflow-review.json").is_file():
        pytest.skip("ruleset template missing")
    payload = json.loads(
        Path(".github/rulesets/independent-workflow-review.json").read_text()
    )
    assert payload["rules"][0]["parameters"]["require_code_owner_review"] is True
    assert payload["rules"][0]["parameters"]["require_last_push_approval"] is True
    assert payload["bypass_actors"] == []


def test_required_privileged_scripts_are_manifest_protected() -> None:
    required = [
        "scripts/github_api.py",
        "scripts/copilot_agent.py",
        "scripts/dispatch_queue.py",
        "scripts/require_planner_plan.py",
        "scripts/priority.py",
        "scripts/project_sync.py",
    ]
    patterns, errors = load_manifest(Path("."))
    assert errors == []
    for script in required:
        assert path_matches_any_pattern(script, patterns), script


def test_all_workflow_discovered_scripts_are_manifest_protected() -> None:
    patterns, errors = load_manifest(Path("."))
    assert errors == []
    files = repository_files(Path("."))
    discovered = discover_workflow_entrypoints(Path("."))
    protected_scripts = {
        path
        for path in files
        if path.startswith("scripts/") and path_matches_any_pattern(path, patterns)
    }
    discovered.update(
        transitive_script_closure(discovered | protected_scripts, Path("."))
    )
    unprotected = [
        script
        for script in sorted(discovered)
        if not path_matches_any_pattern(script, patterns)
    ]
    assert unprotected == []


def test_ruleset_drift_errors_filters_out_ownership_failures() -> None:
    errors = [
        "missing CODEOWNERS: .github/CODEOWNERS",
        "live ruleset: ruleset enforcement must be active",
        "unable to fetch live ruleset: ruleset not found: Require independent review",
        "scripts/dispatch_queue.py has no human CODEOWNERS",
    ]
    assert ruleset_drift_errors(errors) == [
        "live ruleset: ruleset enforcement must be active",
        "unable to fetch live ruleset: ruleset not found: Require independent review",
    ]


def test_report_ruleset_drift_noop_without_drift_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_GOVERNANCE_DRIFT", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        github_api, "api", lambda method, path, **kw: calls.append((method, path))
    )
    report_ruleset_drift("owner/repo", ["missing CODEOWNERS: .github/CODEOWNERS"])
    assert calls == []


def test_report_ruleset_drift_noop_when_not_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_GOVERNANCE_DRIFT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        github_api, "api", lambda method, path, **kw: calls.append((method, path))
    )
    report_ruleset_drift(
        "owner/repo", ["live ruleset: ruleset enforcement must be active"]
    )
    assert calls == []


def test_report_ruleset_drift_creates_issue_when_none_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_GOVERNANCE_DRIFT", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method: str, path: str, *, body: dict | None = None, **kw: object) -> object:
        calls.append((method, path, body))
        if method == "GET":
            return []
        return {"number": 999}

    monkeypatch.setattr(github_api, "api", fake_api)
    report_ruleset_drift(
        "owner/repo", ["live ruleset: ruleset enforcement must be active"]
    )
    creates = [c for c in calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert len(creates) == 1
    assert creates[0][2]["title"] == GOVERNANCE_DRIFT_ISSUE_TITLE
    assert "ruleset enforcement must be active" in creates[0][2]["body"]
    for label_prefix in ("status:", "type:", "priority:", "agent:"):
        assert label_prefix not in creates[0][2]["body"].split("@saberistic")[0]


def test_report_ruleset_drift_comments_on_existing_open_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_GOVERNANCE_DRIFT", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method: str, path: str, *, body: dict | None = None, **kw: object) -> object:
        calls.append((method, path, body))
        if method == "GET":
            return [
                {
                    "number": 42,
                    "title": GOVERNANCE_DRIFT_ISSUE_TITLE,
                }
            ]
        raise AssertionError("must not create a duplicate issue")

    monkeypatch.setattr(github_api, "api", fake_api)
    monkeypatch.setattr(
        github_api,
        "post_issue_comment",
        lambda repo, issue, body: calls.append(("COMMENT", str(issue), {"body": body})),
    )
    report_ruleset_drift(
        "owner/repo", ["live ruleset: ruleset enforcement must be active"]
    )
    comments = [c for c in calls if c[0] == "COMMENT"]
    assert len(comments) == 1
    assert comments[0][1] == "42"
