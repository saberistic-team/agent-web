from __future__ import annotations

import json
from pathlib import Path

import pytest

from validate_workflow_governance import (
    discover_workflow_script_entrypoints,
    discover_script_import_graph,
    fetch_live_ruleset,
    independent_approval_error,
    normalize_pattern,
    transitive_script_closure,
    validate,
    validate_ruleset_payload,
    validate_transitive_coverage,
    validate_workflow_entrypoint_coverage,
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
                "schema_version": 2,
                "protected_paths": [
                    {
                        "path": ".github/workflows/**",
                        "category": "GitHub Actions workflows",
                    }
                ],
                "workflow_entrypoint_exemptions": [],
                "dynamic_script_imports": {},
            }
        )
    )


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    errors = validate(tmp_path)
    assert "manifest pattern missing from CODEOWNERS: .github/workflows/**" in errors
    assert "CODEOWNERS pattern missing from manifest: docs/**" in errors
    assert (
        "unowned protected path: .github/workflows/reviewer.yml "
        "(from .github/workflows/**)"
    ) in errors


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners="/.github/workflows/** @reviewer-bot\n"
    )

    errors = validate(tmp_path)
    assert any("bot CODEOWNER" in error for error in errors)


def test_manifest_codeowners_drift_fails_both_directions(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path,
        codeowners=(
            "/.github/workflows/** @human-owner\n"
            "/scripts/extra.py @human-owner\n"
        ),
    )

    errors = validate(tmp_path)
    assert "CODEOWNERS pattern missing from manifest: scripts/extra.py" in errors


def test_discover_workflow_script_entrypoints_on_real_repo() -> None:
    entrypoints = discover_workflow_script_entrypoints(Path(__file__).resolve().parents[1])
    assert "scripts/dispatch_queue.py" in entrypoints
    assert "scripts/github_api.py" not in entrypoints
    assert "scripts/run_agent.py" in entrypoints
    assert "scripts/check_coverage.py" in entrypoints


def test_workflow_entrypoint_without_coverage_fails(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "dispatch.yml").write_text(
        "jobs:\n  run:\n    steps:\n      - run: python scripts/evil_dispatch.py\n"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "evil_dispatch.py").write_text("# privileged\n")
    (root / ".github" / "CODEOWNERS").write_text("/.github/workflows/** @human-owner\n")
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "protected_paths": [
                    {
                        "path": ".github/workflows/**",
                        "category": "GitHub Actions workflows",
                    }
                ],
                "workflow_entrypoint_exemptions": [],
                "dynamic_script_imports": {},
            }
        )
    )

    errors = validate_workflow_entrypoint_coverage(
        root,
        json.loads((root / ".github" / "workflow-governance-paths.json").read_text()),
        [".github/workflows/**"],
    )
    assert errors == [
        "workflow entrypoint lacks governance coverage: scripts/evil_dispatch.py"
    ]


def test_transitive_privileged_helper_without_coverage_fails(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "dispatch.yml").write_text(
        "jobs:\n  run:\n    steps:\n      - run: python scripts/dispatch_queue.py\n"
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "dispatch_queue.py").write_text("from secret_helper import run\n")
    (scripts / "secret_helper.py").write_text("def run():\n    return 1\n")
    payload = {
        "schema_version": 2,
        "protected_paths": [
            {"path": ".github/workflows/**", "category": "workflows"},
            {"path": "scripts/dispatch_queue.py", "category": "dispatch"},
        ],
        "workflow_entrypoint_exemptions": [],
        "dynamic_script_imports": {},
    }

    errors = validate_transitive_coverage(root, payload, [entry["path"] for entry in payload["protected_paths"]])
    assert errors == [
        "privileged transitive helper lacks governance coverage: scripts/secret_helper.py"
    ]


def test_transitive_import_graph_and_closure(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "a.py").write_text("from b import x\n")
    (scripts / "b.py").write_text("from c import y\n")
    (scripts / "c.py").write_text("VALUE = 1\n")

    graph = discover_script_import_graph(tmp_path)
    assert graph["scripts/a.py"] == {"scripts/b.py"}
    assert graph["scripts/b.py"] == {"scripts/c.py"}
    closure = transitive_script_closure({"scripts/a.py"}, graph)
    assert closure == {"scripts/a.py", "scripts/b.py", "scripts/c.py"}


def test_unlisted_privileged_script_fixture_fails_validation(tmp_path: Path) -> None:
    root = tmp_path
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "dispatch.yml").write_text(
        "jobs:\n  run:\n    steps:\n      - run: python scripts/unlisted_privileged.py\n"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "unlisted_privileged.py").write_text(
        "from github_api import api\n"
    )
    (root / "scripts" / "github_api.py").write_text("def api():\n    pass\n")
    (root / ".github" / "CODEOWNERS").write_text(
        "/.github/workflows/** @human-owner\n"
        "/scripts/github_api.py @human-owner\n"
    )
    (root / ".github" / "workflow-governance-paths.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "protected_paths": [
                    {"path": ".github/workflows/**", "category": "workflows"},
                    {"path": "scripts/github_api.py", "category": "api"},
                ],
                "workflow_entrypoint_exemptions": [],
                "dynamic_script_imports": {},
            }
        )
    )

    errors = validate(root)
    assert "workflow entrypoint lacks governance coverage: scripts/unlisted_privileged.py" in errors


def test_non_human_approval_is_rejected() -> None:
    owners = ["@saberistic", "@mehdidehdar", "@Amirsharifico"]
    assert (
        independent_approval_error(
            "saberistic-agent-web-reviewer", "saberistic", owners
        )
        == "non-human approval rejected: saberistic-agent-web-reviewer"
    )
    assert (
        independent_approval_error("github-actions[bot]", "saberistic", owners)
        == "non-human approval rejected: github-actions[bot]"
    )


def test_author_self_approval_is_rejected() -> None:
    owners = ["@saberistic", "@mehdidehdar", "@Amirsharifico"]
    assert (
        independent_approval_error("saberistic", "saberistic", owners)
        == "author self-approval rejected: saberistic"
    )


def test_independent_human_codeowner_approval_succeeds() -> None:
    owners = ["@saberistic", "@mehdidehdar", "@Amirsharifico"]
    assert independent_approval_error("mehdidehdar", "saberistic", owners) is None
    assert independent_approval_error("Amirsharifico", "saberistic", owners) is None


def test_non_codeowner_human_approval_is_rejected() -> None:
    owners = ["@saberistic", "@mehdidehdar", "@Amirsharifico"]
    assert (
        independent_approval_error("some-other-human", "saberistic", owners)
        == "reviewer is not a CODEOWNER: some-other-human"
    )


def test_live_ruleset_fixture_requires_codeowner_review() -> None:
    expected_parameters = {
        "dismiss_stale_reviews_on_push": True,
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "required_approving_review_count": 1,
        "required_review_thread_resolution": True,
    }
    valid_payload = {
        "name": "Require independent review for workflow governance",
        "enforcement": "active",
        "bypass_actors": [],
        "rules": [{"type": "pull_request", "parameters": expected_parameters}],
    }
    assert validate_ruleset_payload(valid_payload) == []

    bot_only_payload = {
        "name": "Require independent review for workflow governance",
        "enforcement": "disabled",
        "bypass_actors": [],
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    **expected_parameters,
                    "require_code_owner_review": False,
                    "required_approving_review_count": 0,
                },
            }
        ],
    }
    errors = validate_ruleset_payload(bot_only_payload)
    assert any("enforcement must be active" in error for error in errors)
    assert any("require_code_owner_review" in error for error in errors)
    assert any("required_approving_review_count" in error for error in errors)


def test_stale_approval_settings_present_in_checked_in_ruleset() -> None:
    ruleset = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / ".github/rulesets/independent-workflow-review.json"
        ).read_text()
    )
    parameters = ruleset["rules"][0]["parameters"]
    assert parameters["dismiss_stale_reviews_on_push"] is True
    assert parameters["require_last_push_approval"] is True


def test_normalize_pattern_strips_leading_slash() -> None:
    assert normalize_pattern("/.github/workflows/**") == ".github/workflows/**"


def test_fetch_live_ruleset_uses_github_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

    def fake_urlopen(request: object, timeout: int = 0) -> FakeResponse:
        url = getattr(request, "full_url", "")
        calls.append(url)
        if url.endswith("/rulesets"):
            return FakeResponse(
                [{"id": 1, "name": "Require independent review for workflow governance"}]
            )
        if url.endswith("/rulesets/1"):
            return FakeResponse(
                {
                    "name": "Require independent review for workflow governance",
                    "enforcement": "active",
                    "bypass_actors": [],
                    "rules": [
                        {
                            "type": "pull_request",
                            "parameters": {
                                "dismiss_stale_reviews_on_push": True,
                                "require_code_owner_review": True,
                                "require_last_push_approval": True,
                                "required_approving_review_count": 1,
                                "required_review_thread_resolution": True,
                            },
                        }
                    ],
                }
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("validate_workflow_governance.urllib.request.urlopen", fake_urlopen)

    payload = fetch_live_ruleset("saberistic-team/agent-web", "test-token")
    assert payload["enforcement"] == "active"
    assert calls == [
        "https://api.github.com/repos/saberistic-team/agent-web/rulesets",
        "https://api.github.com/repos/saberistic-team/agent-web/rulesets/1",
    ]


def test_live_ruleset_disabled_prints_remediation(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "saberistic-team/agent-web")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_fetch(_repo: str, _token: str) -> dict[str, object]:
        return {
            "name": "Require independent review for workflow governance",
            "enforcement": "disabled",
            "bypass_actors": [],
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "dismiss_stale_reviews_on_push": True,
                        "require_code_owner_review": False,
                        "require_last_push_approval": True,
                        "required_approving_review_count": 0,
                        "required_review_thread_resolution": True,
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "validate_workflow_governance.fetch_live_ruleset", fake_fetch
    )

    from validate_workflow_governance import main

    assert main() == 1
    captured = capsys.readouterr()
    assert "ruleset enforcement must be active" in captured.err
    assert "REMEDIATION:" in captured.err
    assert "rulesets/18975712" in captured.err


def test_real_repo_covers_required_privileged_scripts() -> None:
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / ".github/workflow-governance-paths.json"
        ).read_text()
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
    from validate_workflow_governance import path_is_covered

    for script in required:
        assert path_is_covered(script, patterns), f"missing coverage for {script}"
