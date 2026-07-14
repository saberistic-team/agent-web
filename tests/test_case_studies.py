"""Unit tests for case study data and rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import case_studies


@pytest.mark.unit
def test_load_case_studies_has_required_fields() -> None:
    studies = case_studies.load_case_studies()
    assert len(studies) >= 3
    slugs = {study["slug"] for study in studies}
    assert {"brave", "baxus", "architecture-diagnostic"}.issubset(slugs)
    for study in studies:
        assert study["engagement"] in case_studies.DISCLAIMERS
        for key, _title in case_studies.SECTIONS:
            assert study[key]


@pytest.mark.unit
def test_get_case_study_found_and_missing(tmp_path: Path) -> None:
    data = {
        "studies": [
            {
                "slug": "sample",
                "org": "Sample Org",
                "headline": "Sample headline",
                "engagement": "saberistic",
                "meta_description": "Meta",
                "context": "Context",
                "problem": "Problem",
                "role": "Role",
                "intervention": "Intervention",
                "result": "Result",
                "cta_label": "CTA",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "case-studies.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    found = case_studies.get_case_study("sample", path=path)
    assert found is not None
    assert found["org"] == "Sample Org"
    assert case_studies.get_case_study("missing", path=path) is None


@pytest.mark.unit
def test_load_case_studies_rejects_duplicate_slug(tmp_path: Path) -> None:
    data = {
        "studies": [
            {
                "slug": "dup",
                "org": "A",
                "headline": "H",
                "engagement": "employer",
                "meta_description": "M",
                "context": "C",
                "problem": "P",
                "role": "R",
                "intervention": "I",
                "result": "Res",
                "cta_label": "Go",
                "cta_href": "/brief",
            },
            {
                "slug": "dup",
                "org": "B",
                "headline": "H2",
                "engagement": "employer",
                "meta_description": "M2",
                "context": "C2",
                "problem": "P2",
                "role": "R2",
                "intervention": "I2",
                "result": "Res2",
                "cta_label": "Go",
                "cta_href": "/brief",
            },
        ]
    }
    path = tmp_path / "case-studies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        case_studies.load_case_studies(path=path)


@pytest.mark.unit
def test_render_case_study_page_structure() -> None:
    study = case_studies.get_case_study("brave")
    assert study is not None
    html = case_studies.render_case_study_page(study)
    assert "<title>Brave — Infrastructure for privacy-aligned payments · saberistic</title>" in html
    assert 'name="description"' in html
    assert 'id="problem-title"' in html
    assert 'id="intervention-title"' in html
    assert 'id="result-title"' in html
    assert "Prior employer role" in html
    assert 'href="/brief"' in html
    assert 'data-engagement="employer"' in html


@pytest.mark.unit
def test_render_case_studies_index_lists_all_studies() -> None:
    html_out = case_studies.render_case_studies_index()
    assert "Case studies — saberistic" in html_out
    assert "in progress" not in html_out
    for slug in ("brave", "baxus", "eternis", "spiral-safe", "architecture-diagnostic"):
        assert f'href="/work/{slug}"' in html_out
    assert "Request an Architecture Diagnostic" in html_out
    assert 'rel="canonical" href="https://saberistic.com/case-studies"' in html_out


@pytest.mark.unit
def test_render_case_studies_index_escapes_html(tmp_path: Path) -> None:
    data = {
        "studies": [
            {
                "slug": "xss",
                "org": "Org<script>",
                "headline": "Head<script>",
                "engagement": "saberistic",
                "meta_description": "Meta<script>",
                "context": "Ctx<script>",
                "problem": "Prob<script>",
                "role": "Role<script>",
                "intervention": "Int<script>",
                "result": "Res<script>",
                "cta_label": "CTA<script>",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "case-studies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    rendered = case_studies.render_case_studies_index(path=path)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.unit
def test_render_escapes_html_in_content(tmp_path: Path) -> None:
    data = {
        "studies": [
            {
                "slug": "xss",
                "org": "Org<script>",
                "headline": "Head<script>",
                "engagement": "saberistic",
                "meta_description": "Meta<script>",
                "context": "Ctx<script>",
                "problem": "Prob<script>",
                "role": "Role<script>",
                "intervention": "Int<script>",
                "result": "Res<script>",
                "cta_label": "CTA<script>",
                "cta_href": "/brief",
            }
        ]
    }
    path = tmp_path / "case-studies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    study = case_studies.get_case_study("xss", path=path)
    assert study is not None
    rendered = case_studies.render_case_study_page(study)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.unit
def test_list_featured_slugs() -> None:
    featured = case_studies.list_featured_slugs()
    assert featured == ["brave", "baxus", "eternis"]
