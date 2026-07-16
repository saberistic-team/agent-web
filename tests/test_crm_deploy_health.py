"""Tests for post-merge CRM deployment-health evidence (#280)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crm_deploy_health import (
    build_deploy_health_record,
    issue_requires_deploy_health,
    qualifies_for_non_runtime_exemption,
    require_post_merge_deploy_health,
    run_route_check,
    smoke_checks_passed,
)


@pytest.mark.unit
def test_is_crm_runtime_change_detects_repository_paths() -> None:
    assert issue_requires_deploy_health(["app/crm_service.py"])
    assert issue_requires_deploy_health(["app/repositories/postgres.py"])
    assert not issue_requires_deploy_health(["docs/DEPLOYMENT_HEALTH.md"])


@pytest.mark.unit
def test_non_runtime_exemption_requires_docs_only_paths() -> None:
    files = ["docs/DEPLOYMENT_HEALTH.md", "docs/ACCEPTANCE.md"]
    assert qualifies_for_non_runtime_exemption(files)
    assert not issue_requires_deploy_health(files)


@pytest.mark.unit
def test_non_runtime_exemption_label_overrides_mixed_docs() -> None:
    files = ["docs/DEPLOYMENT_HEALTH.md", "scripts/crm_deploy_health.py"]
    assert qualifies_for_non_runtime_exemption(
        files,
        labels={"evidence-exempt:non-runtime"},
    )
    assert not issue_requires_deploy_health(
        files,
        labels={"evidence-exempt:non-runtime"},
    )


@pytest.mark.unit
def test_build_record_passes_when_smoke_migration_and_logs_ok() -> None:
    record = build_deploy_health_record(
        sha="abc123def456",
        base_url="https://saberistic.com",
        health={"status": "ok", "schema_version": "018"},
        smoke_checks=[{"id": "health", "result": "pass", "note": "ok"}],
        log_inspection={"regressions_found": False, "summary": "clean"},
        linked_issues=[230],
        linked_prs=[250],
    )
    assert record["result"] == "pass"
    assert record["verification_layers"]["post_deploy_functional_health"] == "pass"
    assert record["linked_issues"] == [230]
    assert record["linked_prs"] == [250]


@pytest.mark.unit
def test_build_record_fails_when_smoke_check_fails() -> None:
    record = build_deploy_health_record(
        sha="abc123def456",
        base_url="https://saberistic.com",
        health={"status": "ok", "schema_version": "018"},
        smoke_checks=[{"id": "admin", "result": "fail", "note": "HTTP 500"}],
        log_inspection={"regressions_found": False, "summary": "clean"},
    )
    assert record["result"] == "fail"
    assert record["verification_layers"]["post_deploy_functional_health"] == "fail"


@pytest.mark.unit
def test_smoke_checks_passed_allows_skip() -> None:
    assert smoke_checks_passed(
        [
            {"result": "pass"},
            {"result": "skip"},
        ]
    )
    assert not smoke_checks_passed([{"result": "fail"}])


@pytest.mark.unit
def test_run_route_check_honors_expected_status_without_following_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(url: str, **kwargs):  # noqa: ANN003
        assert url.endswith("/admin")
        return 303, {"location": "/admin/login"}, b""

    monkeypatch.setattr("crm_deploy_health._http_request", fake_request)
    result = run_route_check(
        "https://saberistic.com",
        {
            "id": "admin_dashboard_guard",
            "category": "acquisition_dashboard",
            "method": "GET",
            "path": "/admin",
            "expect_status": {303, 302},
        },
    )
    assert result["result"] == "pass"


@pytest.mark.unit
def test_require_post_merge_deploy_health_blocks_missing_record() -> None:
    issue_payload = {
        "body": "CRM runtime change",
        "labels": [],
    }
    with patch("crm_deploy_health.api", return_value=issue_payload):
        with patch(
            "github_api.list_pr_files",
            return_value=[{"filename": "app/crm_service.py"}],
        ):
            with patch(
                "crm_deploy_health.find_deploy_health_record",
                return_value=None,
            ):
                with pytest.raises(Exception, match="requires post-merge deployment-health"):
                    require_post_merge_deploy_health(
                        "o/r",
                        230,
                        "sha230",
                        pr_number=250,
                    )


@pytest.mark.unit
def test_require_post_merge_deploy_health_blocks_failing_record() -> None:
    issue_payload = {
        "body": "CRM runtime change",
        "labels": [],
    }
    with patch("crm_deploy_health.api", return_value=issue_payload):
        with patch(
            "github_api.list_pr_files",
            return_value=[{"filename": "app/crm_service.py"}],
        ):
            with patch(
                "crm_deploy_health.find_deploy_health_record",
                return_value={"result": "fail", "verification_layers": {}},
            ):
                with pytest.raises(Exception, match="is not passing"):
                    require_post_merge_deploy_health(
                        "o/r",
                        230,
                        "sha230",
                        pr_number=250,
                    )


@pytest.mark.unit
def test_require_post_merge_deploy_health_skips_docs_only_issue() -> None:
    with patch("crm_deploy_health.api") as api_mock:
        api_mock.return_value = {
            "body": "## Evidence exemption\nDocs only\n",
            "labels": [],
        }
        with patch("github_api.list_pr_files", return_value=[{"filename": "docs/X.md"}]):
            result = require_post_merge_deploy_health(
                "o/r",
                280,
                "sha280",
                pr_number=999,
            )
    assert result["required"] is False


@pytest.mark.unit
def test_reconciliation_record_for_issue_230_is_passing() -> None:
    path = Path(".agent/deploy/7c236962e0fc/deploy-health.json")
    assert path.is_file(), "missing backfilled #230 deployment-health record"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["result"] == "pass"
    assert record["sha"].startswith("7c236962e0fc")
    assert 230 in record["linked_issues"]
    assert 250 in record["linked_prs"]
    assert record["migration"]["status"] == "ok"
    assert record["verification_layers"]["post_deploy_functional_health"] == "pass"
    assert "reconcile_note" in record
    assert all(item["result"] in {"pass", "skip"} for item in record["smoke_checks"])


@pytest.mark.unit
def test_main_require_for_close_exits_nonzero_on_missing_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crm_deploy_health import main

    monkeypatch.setattr(
        "crm_deploy_health.require_post_merge_deploy_health",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    assert (
        main(
            [
                "--repo",
                "o/r",
                "--sha",
                "deadbeef",
                "--issue",
                "230",
                "--require-for-close",
            ]
        )
        == 1
    )
