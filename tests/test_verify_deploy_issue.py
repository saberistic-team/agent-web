"""Builder must not treat product ACs as verify-deploy ops shortcuts (#210)."""

from __future__ import annotations

import pytest

from run_agent import is_verify_deploy_issue


@pytest.mark.unit
def test_real_render_smoke_issue_matches() -> None:
    assert is_verify_deploy_issue(
        "Verify production Render deploy (agent-web-hello)",
        (
            "## Verify production Render deploy\n\n"
            "**Production URL:** https://agent-web-hello.onrender.com\n\n"
            "### Acceptance\n"
            "- `GET /health` → `{\"status\":\"ok\"}`\n"
            "- `GET /hello` → `{\"message\":\"hello world\"}`\n\n"
            "Prefer running:\n\n"
            "```bash\npython scripts/smoke_deploy.py\n```\n"
        ),
    )


@pytest.mark.unit
def test_smoke_deploy_script_mentions_match() -> None:
    assert is_verify_deploy_issue(
        "Ops: check production after cutover",
        "Run `python scripts/smoke_deploy.py` against https://saberistic.com",
    )


@pytest.mark.unit
def test_migration_issue_with_verify_and_deploy_ac_does_not_match() -> None:
    """Regression: #210 was closed as done after a false verify-deploy match."""
    assert not is_verify_deploy_issue(
        "Reconcile legacy migration 013 with the canonical pipeline schema",
        (
            "## Summary\n"
            "Add a forward-only migration that reconciles databases…\n\n"
            "## Acceptance criteria\n"
            "- A database representing the earlier applied `013` state upgrades…\n"
            "- The change is ready to deploy in the PR\n\n"
            "## Verification expectations\n"
            "- Verify canonical history row counts and values after legacy "
            "reconciliation.\n"
            "- Verify pipeline mutations and rollback behavior.\n"
            "- Confirm the application no longer encounters missing canonical "
            "pipeline relations or columns.\n"
        ),
    )


@pytest.mark.unit
def test_backup_verify_issue_with_render_mention_does_not_match() -> None:
    assert not is_verify_deploy_issue(
        "Verify CRM and analytics backup and restore procedures",
        (
            "- Document which Render Postgres backups exist and their retention\n"
            "- Verify migrations can advance a restored database safely\n"
        ),
    )


@pytest.mark.unit
def test_title_verify_deploy_without_live_target_does_not_match() -> None:
    assert not is_verify_deploy_issue(
        "Verify production deploy checklist",
        "Write the runbook; do not hit live endpoints yet.",
    )
