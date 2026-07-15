from __future__ import annotations

import json
from pathlib import Path

from validate_workflow_governance import validate


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


def test_repository_workflow_governance_is_fully_owned() -> None:
    assert validate() == []


def test_unowned_protected_path_fails_clearly(tmp_path: Path) -> None:
    _write_policy_repo(tmp_path, codeowners="/docs/** @human-owner\n")

    assert validate(tmp_path) == [
        "unowned protected path: .github/workflows/reviewer.yml "
        "(from .github/workflows/**)"
    ]


def test_bot_cannot_be_protected_path_codeowner(tmp_path: Path) -> None:
    _write_policy_repo(
        tmp_path, codeowners="/.github/workflows/** @reviewer-bot\n"
    )

    assert validate(tmp_path) == [
        "protected path has bot CODEOWNER: .github/workflows/reviewer.yml "
        "(@reviewer-bot)"
    ]
