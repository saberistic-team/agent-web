from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from validate_workflow_governance import (
    discover_workflow_scripts,
    independent_codeowner_approval_satisfies,
    parse_codeowners,
    privileged_script_inventory,
    transitive_script_closure,
    validate,
    validate_privileged_script_coverage,
    validate_ruleset_document,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HUMAN_OWNERS = ("@saberistic", "@mehdidehdar", "@Amirsharifico")
CODEOWNERS_TEMPLATE = "\n".join(
    [
        "# GitHub Actions",
        "/.github/workflows/** " + " ".join(HUMAN_OWNERS),
        "",
        "# GitHub API and control plane",
        "/scripts/github_api.py " + " ".join(HUMAN_OWNERS),
        "/scripts/copilot_agent.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Dispatch and queue orchestration",
        "/scripts/dispatch_queue.py " + " ".join(HUMAN_OWNERS),
        "/scripts/milestones.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Priority selection",
        "/scripts/priority.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Project board sync",
        "/scripts/project_sync.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Planner gate",
        "/scripts/require_planner_plan.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Reviewer automation",
        "/scripts/review_*.py " + " ".join(HUMAN_OWNERS),
        "/scripts/review_decision.py " + " ".join(HUMAN_OWNERS),
        "/scripts/review_models.py " + " ".join(HUMAN_OWNERS),
        "/scripts/acceptance.py " + " ".join(HUMAN_OWNERS),
        "/scripts/check_permission.py " + " ".join(HUMAN_OWNERS),
        "/scripts/pr_labels.py " + " ".join(HUMAN_OWNERS),
        "/scripts/run_agent.py " + " ".join(HUMAN_OWNERS),
        "/scripts/write_trace.py " + " ".join(HUMAN_OWNERS),
        "/scripts/digest_trace.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Builder prompts and configuration",
        "/scripts/codegen_*.py " + " ".join(HUMAN_OWNERS),
        "/scripts/builder_conflicts.py " + " ".join(HUMAN_OWNERS),
        "/scripts/cursor_sdk_patch.py " + " ".join(HUMAN_OWNERS),
        "/scripts/cursor_model.py " + " ".join(HUMAN_OWNERS),
        "/scripts/screenshot_deploy.py " + " ".join(HUMAN_OWNERS),
        "",
        "# Deploy and release",
        "/scripts/render_deploy.py " + " ".join(HUMAN_OWNERS),
        "/scripts/freeze_shipped_migrations.py " + " ".join(HUMAN_OWNERS),
        "/scripts/post_deploy_visual.py " + " ".join(HUMAN_OWNERS),
        "",
        "# CI gates",
        "/scripts/check_coverage.py " + " ".join(HUMAN_OWNERS),
        "",
        "/AGENTS/** " + " ".join(HUMAN_OWNERS),
        "/.github/copilot-instructions.md " + " ".join(HUMAN_OWNERS),
        "",
        "# Repository policy and the self-validating policy checker",
        "/docs/WORKFLOW_GOVERNANCE.md " + " ".join(HUMAN_OWNERS),
        "/docs/IDENTITIES.md " + " ".join(HUMAN_OWNERS),
        "/docs/LABELS.md " + " ".join(HUMAN_OWNERS),
        "/.github/CODEOWNERS " + " ".join(HUMAN_OWNERS),
        "/.github/workflow-governance-paths.json " + " ".join(HUMAN_OWNERS),
        "/.github/rulesets/** " + " ".join(HUMAN_OWNERS),
        "/scripts/validate_workflow_governance.py " + " ".join(HUMAN_OWNERS),
        "/tests/test_workflow_governance.py " + " ".join(HUMAN_OWNERS),
        "",
    ]
)

MANIFEST_TEMPLATE = {
    "schema_version": 1,
    "protected_paths": [
        {"path": ".github/workflows/**", "category": "GitHub Actions workflows"},
        {"path": "scripts/github_api.py", "category": "GitHub API and control plane"},
        {"path": "scripts/copilot_agent.py", "category": "GitHub API and control plane"},
        {"path": "scripts/dispatch_queue.py", "category": "dispatch and queue orchestration"},
        {"path": "scripts/milestones.py", "category": "dispatch and queue orchestration"},
        {"path": "scripts/priority.py", "category": "priority selection"},
        {"path": "scripts/project_sync.py", "category": "project board sync"},
        {"path": "scripts/require_planner_plan.py", "category": "planner gate"},
        {"path": "scripts/review_*.py", "category": "reviewer automation"},
        {"path": "scripts/review_decision.py", "category": "reviewer automation"},
        {"path": "scripts/review_models.py", "category": "reviewer automation"},
        {"path": "scripts/acceptance.py", "category": "reviewer automation"},
        {"path": "scripts/check_permission.py", "category": "reviewer automation"},
        {"path": "scripts/pr_labels.py", "category": "reviewer automation"},
        {"path": "scripts/run_agent.py", "category": "reviewer and Builder orchestration"},
        {"path": "scripts/write_trace.py", "category": "reviewer and Builder audit trail"},
        {"path": "scripts/digest_trace.py", "category": "reviewer and Builder audit trail"},
        {"path": "scripts/codegen_*.py", "category": "Builder configuration"},
        {"path": "scripts/builder_conflicts.py", "category": "Builder configuration"},
        {"path": "scripts/cursor_sdk_patch.py", "category": "Builder configuration"},
        {"path": "scripts/cursor_model.py", "category": "Builder configuration"},
        {"path": "scripts/screenshot_deploy.py", "category": "Builder configuration"},
        {"path": "scripts/render_deploy.py", "category": "deploy and release"},
        {"path": "scripts/freeze_shipped_migrations.py", "category": "deploy and release"},
        {"path": "scripts/post_deploy_visual.py", "category": "deploy and release"},
        {"path": "scripts/check_coverage.py", "category": "CI gates"},
        {"path": "AGENTS/**", "category": "Builder prompts and repository policy"},
        {
            "path": ".github/copilot-instructions.md",
            "category": "Builder prompts and repository policy",
        },
        {"path": "docs/WORKFLOW_GOVERNANCE.md", "category": "repository policy"},
        {"path": "docs/IDENTITIES.md", "category": "repository policy"},
        {"path": "docs/LABELS.md", "category": "repository policy"},
        {"path": ".github/CODEOWNERS", "category": "repository policy"},
        {"path": ".github/workflow-governance-paths.json", "category": "repository policy"},
        {"path": ".github/rulesets/**", "category": "repository policy"},
        {
            "path": "scripts/validate_workflow_governance.py",
            "category": "repository policy",
        },
        {"path": "tests/test_workflow_governance.py", "category": "repository policy"},
    ],
}


def _write_policy_repo(
    root: Path,
    *,
    codeowners: str | None = None,
    manifest: dict | None = None,
    extra_workflow: str | None = None,
    extra_script: str | None = None,
) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "reviewer.yml").write_text(
        "name: Reviewer\nsteps:\n  - run: python scripts/run_agent.py\n"
    )
    if extra_workflow:
        (root / ".github" / "workflows" / "extra.yml").write_text(extra_workflow)
    (root / ".github" / "rulesets").mkdir(parents=True)
    ruleset = REPO_ROOT / ".github" / "rulesets" / "independent-workflow-review.json"
    (root / ".github" / "rulesets" / "independent-workflow-review.json").write_text(
        ruleset.read_text()
    )
    (root / "scripts").mkdir(parents=True)
    for name in (
        "run_agent.py",
        "github_api.py",
        "dispatch_queue.py",
        "milestones.py",
        "priority.py",
        "pr_labels.py",
        "copilot_agent.py",
    ):
        (root / "scripts" / name).write_text(f"# stub {name}\n")
    if extra_script:
        script_name, script_body = extra_script
        (root / "scripts" / script_name).write_text(script_body)
    (root / "AGENTS").mkdir(parents=True)
    (root / "AGENTS" / "builder.md").write_text("# Builder\n")
    (root / "docs").mkdir(parents=True)
    for doc in ("WORKFLOW_GOVERNANCE.md", "IDENTITIES.md", "LABELS.md"):
        (root / "docs" / doc).write_text(f"# {doc}\n")
    (root / ".github" / "copilot-instructions.md").write_text("# Copilot\n")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_workflow_governance.py").write_text("# tests\n")
    (root / ".github" / "CODEOWNERS").write_text(
        codeowners if codeowners is not None else CODEOWNERS_TEMPLATE
    )
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps(manifest if manifest is not None else MANIFEST_TEMPLATE)
    )
    validator = REPO_ROOT / "scripts" / "validate_workflow_governance.py"
    (root / "scripts" / "validate_workflow_governance.py").write_text(
        validator.read_text()
    )


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_discover_workflow_run_commands() -> None:
    scripts = discover_workflow_scripts(REPO_ROOT)
    assert "scripts/run_agent.py" in scripts
    assert "scripts/dispatch_queue.py" in scripts
    assert "scripts/project_sync.py" in scripts
    assert "scripts/github_api.py" not in scripts


def test_transitive_helpers_include_github_api() -> None:
    entrypoints = discover_workflow_scripts(REPO_ROOT)
    closure = transitive_script_closure(entrypoints, REPO_ROOT)
    assert "scripts/github_api.py" in closure
    assert "scripts/priority.py" in closure
    assert "scripts/milestones.py" in closure


def test_privileged_inventory_includes_explicit_dormant_helpers() -> None:
    inventory = privileged_script_inventory(REPO_ROOT)
    assert "scripts/copilot_agent.py" in inventory
    assert "scripts/github_api.py" in inventory


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    errors = validate(tmp_path)
    assert any(
        error.startswith("manifest pattern missing from CODEOWNERS:")
        for error in errors
    )
    assert "unowned protected path: .github/workflows/reviewer.yml (from .github/workflows/**)" in errors
    assert "unowned protected path: scripts/run_agent.py (from scripts/run_agent.py)" in errors


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        codeowners=CODEOWNERS_TEMPLATE.replace("@saberistic", "@reviewer-bot", 1),
    )

    errors = validate(tmp_path)
    assert any("bot CODEOWNER" in error for error in errors)


def test_unlisted_workflow_script_fails_validation(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        extra_workflow="name: Extra\nsteps:\n  - run: python scripts/evil_queue.py\n",
        extra_script=("evil_queue.py", "# evil\n"),
    )
    manifest = json.loads(
        (tmp_path / ".github" / "workflow-governance-paths.json").read_text()
    )
    patterns = [entry["path"] for entry in manifest["protected_paths"]]
    errors = validate_privileged_script_coverage(tmp_path, patterns)
    assert errors == [
        "privileged script lacks governance coverage: scripts/evil_queue.py "
        "(workflow entrypoint or transitive helper)"
    ]


def test_unlisted_transitive_helper_fails_validation(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        extra_script=(
            "run_agent.py",
            "from secret_helper import mutate_github\n",
        ),
    )
    (tmp_path / "scripts" / "secret_helper.py").write_text(
        "def mutate_github():\n    return None\n"
    )
    manifest = json.loads(
        (tmp_path / ".github" / "workflow-governance-paths.json").read_text()
    )
    patterns = [entry["path"] for entry in manifest["protected_paths"]]
    errors = validate_privileged_script_coverage(tmp_path, patterns)
    assert errors == [
        "privileged script lacks governance coverage: scripts/secret_helper.py "
        "(workflow entrypoint or transitive helper)"
    ]


def test_manifest_codeowners_drift_fails_both_directions(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        codeowners=CODEOWNERS_TEMPLATE + "/scripts/evil_queue.py @saberistic\n",
    )
    errors = validate(tmp_path)
    assert "CODEOWNERS pattern missing from manifest: scripts/evil_queue.py" in errors


def test_non_human_approval_is_rejected() -> None:
    rules = parse_codeowners((REPO_ROOT / ".github" / "CODEOWNERS").read_text())
    reviews = [
        {
            "state": "APPROVED",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }
    ]
    ok, reason = independent_codeowner_approval_satisfies(
        reviews,
        pr_author="saberistic",
        changed_paths=["scripts/github_api.py"],
        rules=rules,
    )
    assert ok is False
    assert "bot" in reason


def test_author_self_approval_is_rejected() -> None:
    rules = parse_codeowners((REPO_ROOT / ".github" / "CODEOWNERS").read_text())
    reviews = [{"state": "APPROVED", "user": {"login": "saberistic"}}]
    ok, reason = independent_codeowner_approval_satisfies(
        reviews,
        pr_author="saberistic",
        changed_paths=["scripts/github_api.py"],
        rules=rules,
    )
    assert ok is False
    assert "self-approval" in reason


def test_independent_human_approval_succeeds() -> None:
    rules = parse_codeowners((REPO_ROOT / ".github" / "CODEOWNERS").read_text())
    reviews = [{"state": "APPROVED", "user": {"login": "mehdidehdar"}}]
    ok, reason = independent_codeowner_approval_satisfies(
        reviews,
        pr_author="saberistic",
        changed_paths=["scripts/github_api.py"],
        rules=rules,
    )
    assert ok is True
    assert "mehdidehdar" in reason


def test_ruleset_document_requires_independent_review_settings() -> None:
    assert (
        validate_ruleset_document(
            REPO_ROOT / ".github" / "rulesets" / "independent-workflow-review.json"
        )
        == []
    )


def test_ruleset_document_rejects_weakened_parameters(tmp_path: Path) -> None:
    weakened = {
        "name": "Require independent review for workflow governance",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 0,
                    "required_review_thread_resolution": False,
                },
            }
        ],
    }
    path = tmp_path / "ruleset.json"
    path.write_text(json.dumps(weakened))
    errors = validate_ruleset_document(path)
    assert any("require_code_owner_review" in error for error in errors)
    assert any("dismiss_stale_reviews_on_push" in error for error in errors)


def test_live_ruleset_matches_required_review_settings() -> None:
    try:
        listing = subprocess.run(
            [
                "gh",
                "api",
                "repos/saberistic-team/agent-web/rulesets",
                "--jq",
                '.[] | select(.name == "Require independent review for workflow governance") | .id',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        ruleset_id = listing.stdout.strip()
        if not ruleset_id:
            pytest.skip("live ruleset not installed")
        detail = subprocess.run(
            [
                "gh",
                "api",
                f"repos/saberistic-team/agent-web/rulesets/{ruleset_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"gh CLI or GitHub auth unavailable: {exc}")

    payload = json.loads(detail.stdout)
    assert payload.get("enforcement") == "active"
    pull_request_rules = [
        rule
        for rule in payload.get("rules", [])
        if isinstance(rule, dict) and rule.get("type") == "pull_request"
    ]
    assert pull_request_rules
    parameters = pull_request_rules[0]["parameters"]
    assert parameters["require_code_owner_review"] is True
    assert parameters["dismiss_stale_reviews_on_push"] is True
    assert parameters["require_last_push_approval"] is True
    assert parameters["required_approving_review_count"] == 1
    assert parameters["required_review_thread_resolution"] is True
