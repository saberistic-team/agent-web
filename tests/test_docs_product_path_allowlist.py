"""Docs PR path allowlist for reviewer hard-fails."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_AGENT_PATH = REPO_ROOT / "scripts" / "run_agent.py"


@pytest.fixture(scope="module")
def run_agent_module():
    spec = importlib.util.spec_from_file_location("run_agent_under_test", RUN_AGENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_docs_out_of_scope_allows_spike_and_docs_support(run_agent_module) -> None:
    allowed = [
        "docs/worldgraph/WORLD_DEFINITION.md",
        "docs/worldgraph/WORLD_MANIFEST_V0.md",
        "docs/worldgraph/world-manifest-v0.schema.json",
        "spike/worldgraph/deterministic_extractor.py",
        "spike/worldgraph/manifest_schema.py",
        "tests/test_worldgraph_manifest_v0.py",
        "scripts/run_agent.py",
        "AGENTS/docs.md",
    ]
    assert run_agent_module.docs_out_of_scope_product_paths(allowed) == []


@pytest.mark.unit
def test_docs_out_of_scope_flags_app_site_migrations_and_stray_code(
    run_agent_module,
) -> None:
    flagged = run_agent_module.docs_out_of_scope_product_paths(
        [
            "app/main.py",
            "site/assets/app.js",
            "migrations/023_example.sql",
            "tools/helper.py",
            "docs/ok.md",
            "spike/worldgraph/deterministic_extractor.py",
        ]
    )
    assert flagged == [
        "app/main.py",
        "site/assets/app.js",
        "migrations/023_example.sql",
        "tools/helper.py",
    ]
