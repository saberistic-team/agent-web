"""Guardrails for #44: project brief deferred scope must stay out of the product."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCT_DIRS = (REPO_ROOT / "app", REPO_ROOT / "site")
PROJECT_BRIEF_DOC = REPO_ROOT / "docs" / "PROJECT_BRIEF.md"

DEFERRED_HEADINGS = (
    "Variable pricing / coupons",
    "Full CRM integration",
)

COUPON_PATTERNS = (
    re.compile(r"\bcoupon", re.I),
    re.compile(r"\bpromotion[_-]?code", re.I),
    re.compile(r"\ballow_promotion_codes\b", re.I),
    re.compile(r"\bdiscount[_-]?code", re.I),
)

CRM_PATTERNS = (
    re.compile(r"\bhubspot\b", re.I),
    re.compile(r"\bsalesforce\b", re.I),
    re.compile(r"\bpipedrive\b", re.I),
    re.compile(r"\bcrm[_-]?sync\b", re.I),
)


def _product_files() -> list[Path]:
    files: list[Path] = []
    for root in PRODUCT_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}:
                files.append(path)
    return files


def _scan_patterns(patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    hits: list[str] = []
    for path in _product_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for pattern in patterns:
            if pattern.search(text):
                hits.append(f"{rel}: {pattern.pattern}")
    return hits


def test_project_brief_doc_lists_deferred_scope() -> None:
    assert PROJECT_BRIEF_DOC.is_file(), "docs/PROJECT_BRIEF.md is required"
    body = PROJECT_BRIEF_DOC.read_text(encoding="utf-8")
    assert "## Intentionally deferred" in body
    for heading in DEFERRED_HEADINGS:
        assert heading in body, f"missing deferred item: {heading}"


def test_readme_links_project_brief_doc() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/PROJECT_BRIEF.md" in readme


def test_no_variable_pricing_or_coupons_in_product() -> None:
    hits = _scan_patterns(COUPON_PATTERNS)
    assert not hits, "coupon/pricing patterns found: " + ", ".join(hits)


def test_no_crm_integration_in_product() -> None:
    hits = _scan_patterns(CRM_PATTERNS)
    assert not hits, "CRM integration patterns found: " + ", ".join(hits)
