"""Prefer PR-head screenshot_deploy when COVERAGE_ROOT is set (#167)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_agent import load_screenshot_deploy  # noqa: E402


def test_load_screenshot_deploy_prefers_coverage_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr_scripts = tmp_path / "scripts"
    pr_scripts.mkdir()
    marker = "loaded_from_pr_head_for_167"
    (pr_scripts / "screenshot_deploy.py").write_text(
        f"SOURCE = {marker!r}\n"
        "def capture_pre_dual(*args, **kwargs):\n"
        "    raise NotImplementedError\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COVERAGE_ROOT", str(tmp_path))
    mod = load_screenshot_deploy()
    assert getattr(mod, "SOURCE") == marker


def test_load_screenshot_deploy_falls_back_to_repo_scripts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COVERAGE_ROOT", raising=False)
    mod = load_screenshot_deploy()
    assert hasattr(mod, "capture_pre_dual")
    assert hasattr(mod, "VIEWPORTS")
