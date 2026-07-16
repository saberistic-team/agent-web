from __future__ import annotations

import json
from pathlib import Path

import pytest

from validate_workflow_governance import (
    discover_required_privileged_scripts,
    discover_transitive_script_helpers,
    discover_workflow_scripts,
    evaluate_independent_codeowner_review,
    human_codeowner_logins,
    parse_codeowners,
    validate,
    validate_discovered_script_coverage,
    validate_manifest_codeowners_sync,
    validate_ruleset_document,
)


HUMAN_OWNERS = "@saberistic @mehdidehdar @Amirsharifico"


def _write_policy_repo(
    root: Path,
    *,
    codeowners: str | None = None,
    manifest_paths: list[dict[str, str]] | None = None,
    workflow_run: str | None = None,
    extra_script: str | None = None,
) -> None:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "rulesets").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)

    workflow_body = workflow_run or "name: Reviewer\n"
    (root / ".github" / "workflows" / "reviewer.yml").write_text(workflow_body)
    (root / ".github" / "rulesets" / "independent-workflow-review.json").write_text(
        (Path(__file__).resolve().parents[1] / ".github" / "rulesets" / "independent-workflow-review.json").read_text()
    )

    if manifest_paths is None:
        manifest_paths = [
            {
                "path": ".github/workflows/**",
                "category": "GitHub Actions workflows",
            }
        ]

    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps({"schema_version": 1, "protected_paths": manifest_paths})
    )

    if codeowners is None:
        codeowners = f"/.github/workflows/** {HUMAN_OWNERS}\n"
    (root / ".github" / "CODEOWNERS").write_text(codeowners)

    (root / "scripts" / "validate_workflow_governance.py").write_text("# stub\n")
    (root / "tests" / "test_workflow_governance.py").write_text("# stub\n")

    if extra_script is not None:
        (root / "scripts" / "unlisted_helper.py").write_text(extra_script)


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @saberistic\n")

    assert any(
        "unowned protected path: .github/workflows/reviewer.yml" in error
        for error in validate(tmp_path)
    )


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners=f"/.github/workflows/** @reviewer-bot\n"
    )

    assert any(
        "protected path has bot CODEOWNER" in error for error in validate(tmp_path)
    )


def test_workflow_scripts_are_discovered_from_real_repo() -> None:
    scripts = discover_workflow_scripts(Path(__file__).resolve().parents[1])
    assert "scripts/dispatch_queue.py" in scripts
    assert "scripts/project_sync.py" in scripts
    assert "scripts/require_planner_plan.py" in scripts


def test_transitive_helpers_include_github_api_and_priority() -> None:
    root = Path(__file__).resolve().parents[1]
    seeds = discover_workflow_scripts(root)
    helpers = discover_transitive_script_helpers(root, seeds)
    assert "scripts/github_api.py" in helpers
    assert "scripts/priority.py" in helpers
    assert "scripts/milestones.py" in helpers
    assert "scripts/screenshot_deploy.py" in helpers
    assert "scripts/smoke_deploy.py" in helpers


def test_required_privileged_scripts_cover_issue_minimums() -> None:
    root = Path(__file__).resolve().parents[1]
    patterns = [
        entry["path"]
        for entry in json.loads(
            (root / ".github" / "workflow-governance-paths.json").read_text()
        )["protected_paths"]
    ]
    required = discover_required_privileged_scripts(root, patterns)
    for script in (
        "scripts/github_api.py",
        "scripts/copilot_agent.py",
        "scripts/dispatch_queue.py",
        "scripts/require_planner_plan.py",
        "scripts/priority.py",
        "scripts/project_sync.py",
    ):
        assert script in required


def test_unlisted_workflow_script_fails_validation(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        workflow_run=(
            "name: Dispatch\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: python scripts/unlisted_helper.py\n"
        ),
        extra_script="from github_api import api\n",
    )
    (tmp_path / "scripts" / "github_api.py").write_text("# privileged helper\n")

    errors = validate_discovered_script_coverage(
        tmp_path,
        [entry["path"] for entry in json.loads((tmp_path / ".github" / "workflow-governance-paths.json").read_text())["protected_paths"]],
    )
    assert errors == [
        "privileged script lacks governance coverage: scripts/github_api.py "
        "(add to workflow-governance-paths.json and CODEOWNERS)",
        "privileged script lacks governance coverage: scripts/unlisted_helper.py "
        "(add to workflow-governance-paths.json and CODEOWNERS)",
    ]


def test_moving_privileged_logic_into_unprotected_helper_fails(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        manifest_paths=[
            {
                "path": ".github/workflows/**",
                "category": "GitHub Actions workflows",
            },
            {
                "path": "scripts/run_agent.py",
                "category": "orchestration",
            },
        ],
        codeowners=(
            f"/.github/workflows/** {HUMAN_OWNERS}\n"
            f"/scripts/run_agent.py {HUMAN_OWNERS}\n"
        ),
        workflow_run=(
            "name: Builder\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: python scripts/run_agent.py\n"
        ),
    )
    (tmp_path / "scripts" / "run_agent.py").write_text(
        "from secret_helper import mutate\n"
    )
    (tmp_path / "scripts" / "secret_helper.py").write_text(
        "from github_api import api\n"
    )
    (tmp_path / "scripts" / "github_api.py").write_text("# helper\n")

    patterns = [
        entry["path"]
        for entry in json.loads(
            (tmp_path / ".github" / "workflow-governance-paths.json").read_text()
        )["protected_paths"]
    ]
    errors = validate_discovered_script_coverage(tmp_path, patterns)
    assert any("scripts/secret_helper.py" in error for error in errors)
    assert any("scripts/github_api.py" in error for error in errors)


def test_manifest_codeowners_drift_fails_in_both_directions(tmp_path: Path) -> None:
    rules = parse_codeowners(f"/.github/workflows/** {HUMAN_OWNERS}\n")
    patterns = [".github/workflows/**"]
    assert validate_manifest_codeowners_sync(patterns, rules) == []

    extra_rules = parse_codeowners(
        f"/.github/workflows/** {HUMAN_OWNERS}\n"
        f"/scripts/github_api.py {HUMAN_OWNERS}\n"
    )
    assert validate_manifest_codeowners_sync(patterns, extra_rules) == [
        "CODEOWNERS pattern missing from manifest: /scripts/github_api.py"
    ]

    extra_patterns = [".github/workflows/**", "scripts/github_api.py"]
    assert validate_manifest_codeowners_sync(extra_patterns, rules) == [
        "manifest pattern missing from CODEOWNERS: /scripts/github_api.py"
    ]


def test_repository_manifest_and_codeowners_are_in_sync() -> None:
    root = Path(__file__).resolve().parents[1]
    patterns = [
        entry["path"]
        for entry in json.loads(
            (root / ".github" / "workflow-governance-paths.json").read_text()
        )["protected_paths"]
    ]
    rules = parse_codeowners((root / ".github" / "CODEOWNERS").read_text())
    assert validate_manifest_codeowners_sync(patterns, rules) == []


def test_non_human_approval_is_rejected() -> None:
    rules = parse_codeowners((Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text())
    humans = human_codeowner_logins(rules)
    errors = evaluate_independent_codeowner_review(
        pr_author="builder-author",
        reviews=[
            {
                "state": "APPROVED",
                "submitted_at": "2026-07-16T00:00:00Z",
                "commit_id": "abc123",
                "user": {"login": "saberistic-agent-web-reviewer[bot]"},
            }
        ],
        codeowner_logins=humans,
        head_sha="abc123",
    )
    assert errors == [
        "protected-path PR lacks independent human CODEOWNER approval "
        "(bot, author, stale, or non-owner reviews do not count)"
    ]


def test_independent_human_approval_succeeds() -> None:
    rules = parse_codeowners((Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text())
    humans = human_codeowner_logins(rules)
    assert (
        evaluate_independent_codeowner_review(
            pr_author="builder-author",
            reviews=[
                {
                    "state": "APPROVED",
                    "submitted_at": "2026-07-16T00:00:00Z",
                    "commit_id": "abc123",
                    "user": {"login": "saberistic"},
                }
            ],
            codeowner_logins=humans,
            head_sha="abc123",
        )
        == []
    )


def test_author_self_approval_does_not_satisfy_rule() -> None:
    rules = parse_codeowners((Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text())
    humans = human_codeowner_logins(rules)
    errors = evaluate_independent_codeowner_review(
        pr_author="saberistic",
        reviews=[
            {
                "state": "APPROVED",
                "submitted_at": "2026-07-16T00:00:00Z",
                "commit_id": "abc123",
                "user": {"login": "saberistic"},
            }
        ],
        codeowner_logins=humans,
        head_sha="abc123",
    )
    assert errors


def test_stale_approval_after_material_changes_is_rejected() -> None:
    rules = parse_codeowners((Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS").read_text())
    humans = human_codeowner_logins(rules)
    errors = evaluate_independent_codeowner_review(
        pr_author="builder-author",
        reviews=[
            {
                "state": "APPROVED",
                "submitted_at": "2026-07-16T00:00:00Z",
                "commit_id": "old-sha",
                "user": {"login": "mehdidehdar"},
            }
        ],
        codeowner_logins=humans,
        head_sha="new-sha",
    )
    assert errors


@pytest.mark.parametrize(
    "mutated_field,mutated_value",
    [
        ("require_code_owner_review", False),
        ("required_approving_review_count", 0),
        ("dismiss_stale_reviews_on_push", False),
    ],
)
def test_ruleset_spec_rejects_weakened_review_settings(
    mutated_field: str, mutated_value: object
) -> None:
    root = Path(__file__).resolve().parents[1]
    ruleset = json.loads(
        (root / ".github" / "rulesets" / "independent-workflow-review.json").read_text()
    )
    ruleset["rules"][0]["parameters"][mutated_field] = mutated_value
    errors = validate_ruleset_document(ruleset)
    assert any(mutated_field in error for error in errors)


def test_checked_in_ruleset_spec_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    ruleset = json.loads(
        (root / ".github" / "rulesets" / "independent-workflow-review.json").read_text()
    )
    assert validate_ruleset_document(ruleset) == []


def test_live_ruleset_validation_accepts_checked_in_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    ruleset = json.loads(
        (root / ".github" / "rulesets" / "independent-workflow-review.json").read_text()
    )

    def fake_fetch(_repo: str, _token: str) -> dict[str, object]:
        return ruleset

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        "validate_workflow_governance.fetch_live_ruleset", fake_fetch
    )

    from validate_workflow_governance import validate_live_ruleset

    assert validate_live_ruleset("saberistic-team/agent-web", "test-token") == []
