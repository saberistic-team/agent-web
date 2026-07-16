from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from validate_workflow_governance import (
    discover_workflow_scripts,
    evaluate_independent_review,
    fetch_live_ruleset,
    parse_codeowners,
    transitive_privileged_scripts,
    validate,
    validate_live_ruleset,
    validate_ruleset_export,
)

_ACTIVE_RULESET = {
    "enforcement": "active",
    "bypass_actors": [],
    "rules": [
        {
            "type": "pull_request",
            "parameters": {
                "require_code_owner_review": True,
                "required_approving_review_count": 1,
                "require_last_push_approval": True,
                "dismiss_stale_reviews_on_push": True,
                "required_review_thread_resolution": True,
            },
        }
    ],
}


def _write_policy_repo(
    root: Path,
    *,
    codeowners: str = "/.github/workflows/** @human-owner\n",
    extra_manifest_paths: list[dict[str, str]] | None = None,
    workflow_body: str = "name: Reviewer\n",
    extra_scripts: dict[str, str] | None = None,
) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "reviewer.yml").write_text(workflow_body)
    (root / ".github" / "CODEOWNERS").write_text(codeowners)
    protected_paths = [
        {
            "path": ".github/workflows/**",
            "category": "GitHub Actions workflows",
        }
    ]
    if extra_manifest_paths:
        protected_paths.extend(extra_manifest_paths)
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps({"schema_version": 1, "protected_paths": protected_paths})
    )
    (root / ".github" / "rulesets").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "rulesets" / "independent-workflow-review.json").write_text(
        json.dumps(
            {
                "name": "Require independent review for workflow governance",
                "target": "branch",
                **_ACTIVE_RULESET,
            }
        )
    )
    if extra_scripts:
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        for name, body in extra_scripts.items():
            (scripts / name).write_text(body)


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate(check_live_ruleset=False) == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    errors = validate(tmp_path, check_live_ruleset=False)
    assert (
        "unowned protected path: .github/workflows/reviewer.yml "
        "(from .github/workflows/**)"
    ) in errors


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners="/.github/workflows/** @reviewer-bot\n"
    )

    assert validate(tmp_path, check_live_ruleset=False) == [
        "protected path has bot CODEOWNER: .github/workflows/reviewer.yml "
        "(@reviewer-bot)"
    ]


def test_workflow_scripts_are_discovered_from_actions_yaml(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        workflow_body=(
            "name: Dispatch\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: python scripts/dispatch_queue.py\n"
        ),
        extra_scripts={
            "dispatch_queue.py": "from github_api import api\n",
            "github_api.py": "def api():\n    return None\n",
        },
        extra_manifest_paths=[
            {"path": "scripts/dispatch_queue.py", "category": "dispatch"},
            {"path": "scripts/github_api.py", "category": "control plane"},
        ],
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/dispatch_queue.py @human-owner\n"
            "/scripts/github_api.py @human-owner\n"
        ),
    )

    discovered = discover_workflow_scripts(tmp_path)
    assert discovered == {"scripts/dispatch_queue.py"}
    assert validate(tmp_path, check_live_ruleset=False) == []


def test_transitive_helpers_must_be_governed(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        workflow_body=(
            "name: Builder\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: python scripts/run_agent.py\n"
        ),
        extra_scripts={
            "run_agent.py": "from github_api import api\n",
            "github_api.py": "def api():\n    return None\n",
        },
        extra_manifest_paths=[
            {"path": "scripts/run_agent.py", "category": "orchestration"},
        ],
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/run_agent.py @human-owner\n"
        ),
    )

    closure = transitive_privileged_scripts(
        tmp_path, {"scripts/run_agent.py"}
    )
    assert closure == {"scripts/github_api.py", "scripts/run_agent.py"}
    assert validate(tmp_path, check_live_ruleset=False) == [
        "privileged transitive helper is not governed: scripts/github_api.py"
    ]


def test_unlisted_workflow_invoked_script_fails_validation(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        workflow_body=(
            "name: Planner\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: python scripts/require_planner_plan.py\n"
        ),
        extra_scripts={"require_planner_plan.py": "def main():\n    pass\n"},
        codeowners="/.github/workflows/** @human-owner\n",
    )

    errors = validate(tmp_path, check_live_ruleset=False)
    assert (
        "workflow-invoked script is not governed: scripts/require_planner_plan.py"
        in errors
    )


def test_codeowners_manifest_drift_fails_in_both_directions(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        extra_manifest_paths=[
            {"path": "scripts/github_api.py", "category": "control plane"},
        ],
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/dispatch_queue.py @human-owner\n"
        ),
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "github_api.py").write_text("pass\n")
    (tmp_path / "scripts" / "dispatch_queue.py").write_text("pass\n")

    errors = validate(tmp_path, check_live_ruleset=False)
    assert "manifest pattern missing from CODEOWNERS: scripts/github_api.py" in errors
    assert "CODEOWNERS pattern missing from manifest: scripts/dispatch_queue.py" in errors


def test_bot_approval_is_rejected_for_protected_path_changes() -> None:
    rules = parse_codeowners(
        "/.github/workflows/** @human-owner @other-human\n"
    )
    errors = evaluate_independent_review(
        author="human-owner",
        reviews=[
            {
                "state": "APPROVED",
                "user": {"login": "saberistic-agent-web-reviewer"},
                "commit_id": "abc123",
            }
        ],
        last_push_sha="abc123",
        changed_paths=[".github/workflows/builder.yml"],
        manifest_path_patterns=[".github/workflows/**"],
        rules=rules,
    )
    assert errors == [
        "protected path change lacks independent human CODEOWNER approval "
        "(author='human-owner', qualifying_humans=['human-owner', 'other-human'])"
    ]


def test_author_self_approval_is_rejected_for_protected_path_changes() -> None:
    rules = parse_codeowners(
        "/.github/workflows/** @human-owner @other-human\n"
    )
    errors = evaluate_independent_review(
        author="human-owner",
        reviews=[
            {
                "state": "APPROVED",
                "user": {"login": "human-owner"},
                "commit_id": "abc123",
            }
        ],
        last_push_sha="abc123",
        changed_paths=[".github/workflows/builder.yml"],
        manifest_path_patterns=[".github/workflows/**"],
        rules=rules,
    )
    assert len(errors) == 1
    assert "lacks independent human CODEOWNER approval" in errors[0]


def test_stale_approval_after_material_push_is_rejected() -> None:
    rules = parse_codeowners(
        "/scripts/github_api.py @human-owner @other-human\n"
    )
    errors = evaluate_independent_review(
        author="human-owner",
        reviews=[
            {
                "state": "APPROVED",
                "user": {"login": "other-human"},
                "commit_id": "old-sha",
            }
        ],
        last_push_sha="new-sha",
        changed_paths=["scripts/github_api.py"],
        manifest_path_patterns=["scripts/github_api.py"],
        rules=rules,
    )
    assert len(errors) == 1
    assert "lacks independent human CODEOWNER approval" in errors[0]


def test_independent_human_codeowner_approval_succeeds() -> None:
    rules = parse_codeowners(
        "/scripts/dispatch_queue.py @human-owner @other-human\n"
    )
    assert (
        evaluate_independent_review(
            author="human-owner",
            reviews=[
                {
                    "state": "APPROVED",
                    "user": {"login": "other-human"},
                    "commit_id": "abc123",
                }
            ],
            last_push_sha="abc123",
            changed_paths=["scripts/dispatch_queue.py"],
            manifest_path_patterns=["scripts/dispatch_queue.py"],
            rules=rules,
        )
        == []
    )


def test_non_protected_path_changes_do_not_require_codeowner_approval() -> None:
    rules = parse_codeowners("/scripts/github_api.py @human-owner\n")
    assert (
        evaluate_independent_review(
            author="human-owner",
            reviews=[],
            last_push_sha="abc123",
            changed_paths=["app/main.py"],
            manifest_path_patterns=["scripts/github_api.py"],
            rules=rules,
        )
        == []
    )


def test_ruleset_export_requires_active_codeowner_review() -> None:
    assert validate_ruleset_export(_ACTIVE_RULESET) == []

    disabled = dict(_ACTIVE_RULESET)
    disabled["enforcement"] = "disabled"
    assert any(
        "enforcement must be 'active'" in error
        for error in validate_ruleset_export(disabled)
    )

    weak = json.loads(json.dumps(_ACTIVE_RULESET))
    weak["rules"][0]["parameters"]["require_code_owner_review"] = False
    assert any(
        "require_code_owner_review must be True" in error
        for error in validate_ruleset_export(weak)
    )


def test_live_ruleset_fixture_detects_disabled_enforcement() -> None:
    live_export = {
        "enforcement": "disabled",
        "bypass_actors": [],
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "require_code_owner_review": False,
                    "required_approving_review_count": 0,
                    "require_last_push_approval": True,
                    "dismiss_stale_reviews_on_push": True,
                    "required_review_thread_resolution": True,
                },
            }
        ],
    }
    errors = validate_ruleset_export(live_export)
    assert any("enforcement must be 'active'" in error for error in errors)
    assert any(
        "require_code_owner_review must be True" in error for error in errors
    )


def test_fetch_live_ruleset_returns_matching_ruleset() -> None:
    with patch(
        "validate_workflow_governance._github_api",
        side_effect=[
            [{"id": 18975712, "name": "Require independent review for workflow governance"}],
            _ACTIVE_RULESET,
        ],
    ) as api_mock:
        live = fetch_live_ruleset("saberistic-team/agent-web", "token")

    assert live == _ACTIVE_RULESET
    assert api_mock.call_count == 2
    assert api_mock.call_args_list[0].args == ("token", "/repos/saberistic-team/agent-web/rulesets")
    assert api_mock.call_args_list[1].args == (
        "token",
        "/repos/saberistic-team/agent-web/rulesets/18975712",
    )


def test_fetch_live_ruleset_returns_none_when_missing() -> None:
    with patch(
        "validate_workflow_governance._github_api",
        return_value=[{"id": 1, "name": "Other ruleset"}],
    ):
        assert fetch_live_ruleset("saberistic-team/agent-web", "token") is None


def test_validate_live_ruleset_reports_disabled_export(tmp_path: Path, monkeypatch) -> None:
    _write_policy_repo(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "saberistic-team/agent-web")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    disabled = dict(_ACTIVE_RULESET)
    disabled["enforcement"] = "disabled"
    with patch(
        "validate_workflow_governance.fetch_live_ruleset",
        return_value=disabled,
    ):
        errors = validate_live_ruleset(tmp_path)

    assert any("live ruleset drift:" in error for error in errors)
    assert any("enforcement must be 'active'" in error for error in errors)


def test_validate_live_ruleset_passes_when_active(tmp_path: Path, monkeypatch) -> None:
    _write_policy_repo(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "saberistic-team/agent-web")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    with patch(
        "validate_workflow_governance.fetch_live_ruleset",
        return_value=_ACTIVE_RULESET,
    ):
        assert validate_live_ruleset(tmp_path) == []


def test_validate_live_ruleset_skips_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert validate_live_ruleset() == []


def test_repository_discovers_privileged_scripts_from_real_workflows() -> None:
    discovered = discover_workflow_scripts(Path(__file__).resolve().parents[1])
    assert "scripts/dispatch_queue.py" in discovered
    assert "scripts/github_api.py" not in discovered
    assert "scripts/project_sync.py" in discovered
    assert "scripts/require_planner_plan.py" in discovered


def test_issue_275_target_scripts_are_protected() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / ".github" / "workflow-governance-paths.json").read_text(encoding="utf-8")
    )
    patterns = [entry["path"] for entry in manifest["protected_paths"]]
    required = [
        "scripts/github_api.py",
        "scripts/copilot_agent.py",
        "scripts/dispatch_queue.py",
        "scripts/require_planner_plan.py",
        "scripts/priority.py",
        "scripts/project_sync.py",
    ]
    for script in required:
        assert script in patterns, f"{script} must be in governance manifest"
