from __future__ import annotations

import json
from pathlib import Path

from validate_workflow_governance import (
    PullRequestReview,
    discover_privileged_scripts,
    discover_transitive_script_helpers,
    discover_workflow_entrypoints,
    evaluate_merge_authorization,
    parse_codeowners,
    validate,
    validate_privileged_script_coverage,
    validate_ruleset_export,
)


def _write_policy_repo(
    root: Path, *, codeowners: str = "/.github/workflows/** @human-owner\n"
) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "reviewer.yml").write_text("name: Reviewer\n")
    (root / ".github" / "CODEOWNERS").write_text(codeowners)
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protected_paths": [
                    {
                        "path": ".github/workflows/**",
                        "category": "GitHub Actions workflows",
                    }
                ],
            }
        )
    )


def _write_minimal_governance_repo(root: Path) -> None:
    """Fixture with synchronized manifest/CODEOWNERS and one workflow script."""
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "dispatch_queue.py").write_text(
        "from github_api import api\n"
    )
    (root / "scripts" / "github_api.py").write_text("def api():\n    return None\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "dispatch.yml").write_text(
        "steps:\n  - run: python scripts/dispatch_queue.py\n"
    )
    manifest = {
        "schema_version": 1,
        "protected_paths": [
            {"path": ".github/workflows/**", "category": "workflows"},
            {"path": "scripts/dispatch_queue.py", "category": "dispatch"},
            {"path": "scripts/github_api.py", "category": "github api"},
        ],
    }
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps(manifest)
    )
    (root / ".github" / "CODEOWNERS").write_text(
        "\n".join(
            [
                "/.github/workflows/** @human-owner",
                "/scripts/dispatch_queue.py @human-owner",
                "/scripts/github_api.py @human-owner",
            ]
        )
        + "\n"
    )


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    assert validate(tmp_path) == [
        "manifest pattern missing from CODEOWNERS: .github/workflows/**",
        "CODEOWNERS pattern missing from manifest: docs/**",
        "unowned protected path: .github/workflows/reviewer.yml "
        "(from .github/workflows/**)",
    ]


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners="/.github/workflows/** @reviewer-bot\n"
    )

    errors = validate(tmp_path)
    assert "protected path has bot CODEOWNER: .github/workflows/reviewer.yml (@reviewer-bot)" in errors


def test_discovers_workflow_run_commands() -> None:
    entrypoints = discover_workflow_entrypoints(Path.cwd())
    assert "scripts/dispatch_queue.py" in entrypoints
    assert "scripts/run_agent.py" in entrypoints
    assert "scripts/project_sync.py" in entrypoints
    assert "scripts/validate_workflow_governance.py" in entrypoints


def test_transitive_helpers_included_in_closure() -> None:
    entrypoints = {"scripts/dispatch_queue.py"}
    closure = discover_transitive_script_helpers(Path.cwd(), entrypoints)
    assert "scripts/github_api.py" in closure
    assert "scripts/milestones.py" in closure
    assert "scripts/priority.py" in closure


def test_unlisted_privileged_script_fails_validation(tmp_path: Path) -> None:
    _write_minimal_governance_repo(tmp_path)
    (tmp_path / "scripts" / "secret_gate.py").write_text("def run():\n    return 1\n")
    (tmp_path / ".github" / "workflows" / "gate.yml").write_text(
        "steps:\n  - run: python scripts/secret_gate.py\n"
    )

    errors = validate_privileged_script_coverage(
        tmp_path,
        [
            ".github/workflows/**",
            "scripts/dispatch_queue.py",
            "scripts/github_api.py",
        ],
    )
    assert errors == [
        "privileged script missing from manifest: scripts/secret_gate.py "
        "(add to workflow-governance-paths.json and CODEOWNERS)"
    ]


def test_moving_logic_to_unlisted_helper_fails_validation(tmp_path: Path) -> None:
    _write_minimal_governance_repo(tmp_path)
    (tmp_path / "scripts" / "hidden_helper.py").write_text("def mutate():\n    return 1\n")
    (tmp_path / "scripts" / "dispatch_queue.py").write_text(
        "from hidden_helper import mutate\n"
    )

    errors = validate_privileged_script_coverage(
        tmp_path,
        [
            ".github/workflows/**",
            "scripts/dispatch_queue.py",
            "scripts/github_api.py",
        ],
    )
    assert errors == [
        "privileged script missing from manifest: scripts/hidden_helper.py "
        "(add to workflow-governance-paths.json and CODEOWNERS)"
    ]


def test_manifest_codeowners_drift_fails_validation(tmp_path: Path) -> None:
    _write_minimal_governance_repo(tmp_path)
    codeowners = (tmp_path / ".github" / "CODEOWNERS").read_text()
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        codeowners + "/scripts/extra.py @human-owner\n"
    )

    errors = validate(tmp_path)
    assert "CODEOWNERS pattern missing from manifest: scripts/extra.py" in errors


def test_non_human_approval_rejected() -> None:
    rules = parse_codeowners((Path.cwd() / ".github" / "CODEOWNERS").read_text())
    errors = evaluate_merge_authorization(
        pr_author="builder-agent",
        changed_files=["scripts/dispatch_queue.py"],
        reviews=[
            PullRequestReview(
                login="saberistic-agent-web-reviewer[bot]",
                state="APPROVED",
                submitted_at="2026-07-16T12:00:00Z",
            )
        ],
        protected_patterns=["scripts/dispatch_queue.py"],
        last_push_at="2026-07-16T11:00:00Z",
        rules=rules,
    )
    assert any("non-human approval" in error for error in errors)
    assert any("requires one independent human CODEOWNER approval" in error for error in errors)


def test_author_self_approval_rejected() -> None:
    rules = parse_codeowners((Path.cwd() / ".github" / "CODEOWNERS").read_text())
    errors = evaluate_merge_authorization(
        pr_author="saberistic",
        changed_files=["scripts/github_api.py"],
        reviews=[
            PullRequestReview(
                login="saberistic",
                state="APPROVED",
                submitted_at="2026-07-16T12:00:00Z",
            )
        ],
        protected_patterns=["scripts/github_api.py"],
        last_push_at="2026-07-16T11:00:00Z",
        rules=rules,
    )
    assert any("author self-approval" in error for error in errors)


def test_stale_approval_rejected() -> None:
    rules = parse_codeowners((Path.cwd() / ".github" / "CODEOWNERS").read_text())
    errors = evaluate_merge_authorization(
        pr_author="builder-agent",
        changed_files=["scripts/github_api.py"],
        reviews=[
            PullRequestReview(
                login="mehdidehdar",
                state="APPROVED",
                submitted_at="2026-07-16T12:00:00Z",
                stale=True,
            )
        ],
        protected_patterns=["scripts/github_api.py"],
        last_push_at="2026-07-16T11:00:00Z",
        rules=rules,
    )
    assert any("stale approval" in error for error in errors)


def test_independent_human_approval_succeeds() -> None:
    rules = parse_codeowners((Path.cwd() / ".github" / "CODEOWNERS").read_text())
    errors = evaluate_merge_authorization(
        pr_author="builder-agent",
        changed_files=["scripts/project_sync.py"],
        reviews=[
            PullRequestReview(
                login="Amirsharifico",
                state="APPROVED",
                submitted_at="2026-07-16T12:00:00Z",
            )
        ],
        protected_patterns=["scripts/project_sync.py"],
        last_push_at="2026-07-16T11:00:00Z",
        rules=rules,
    )
    assert errors == []


def test_ruleset_export_has_required_parameters() -> None:
    ruleset = json.loads(
        (Path.cwd() / ".github/rulesets/independent-workflow-review.json").read_text()
    )
    assert validate_ruleset_export(ruleset) == []


def test_privileged_boundary_covers_required_scripts() -> None:
    privileged = discover_privileged_scripts(Path.cwd())
    required = {
        "scripts/github_api.py",
        "scripts/dispatch_queue.py",
        "scripts/require_planner_plan.py",
        "scripts/priority.py",
        "scripts/project_sync.py",
    }
    assert required.issubset(privileged)
