"""Unit tests for case study data and rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import case_studies
from app.main import app

client = TestClient(app)


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
    assert 'property="og:type" content="website"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '"@type":"WebPage"' in html
    assert 'id="problem-title"' in html
    assert 'id="intervention-title"' in html
    assert 'id="result-title"' in html
    assert "Prior employer role" in html
    assert 'href="/brief"' in html
    assert 'data-engagement="employer"' in html


@pytest.mark.unit
@pytest.mark.parametrize("slug", [study["slug"] for study in case_studies.load_case_studies()])
def test_case_study_routes_have_complete_metadata(slug: str) -> None:
    study = case_studies.get_case_study(slug)
    assert study is not None
    response = client.get(f"/work/{slug}")
    assert response.status_code == 200
    body = response.text
    page_title = case_studies.case_study_page_title(study)
    canonical = case_studies.case_study_canonical_url(slug)
    assert f"<title>{page_title}</title>" in body
    assert f'content="{study["meta_description"]}"' in body
    assert f'rel="canonical" href="{canonical}"' in body
    assert f'property="og:title" content="{page_title}"' in body
    assert f'property="og:description" content="{study["meta_description"]}"' in body
    assert f'property="og:url" content="{canonical}"' in body
    assert 'property="og:type" content="website"' in body
    assert 'property="og:site_name" content="saberistic"' in body
    assert 'property="og:image" content="https://saberistic.com/assets/og-share.png"' in body
    assert 'property="og:image:width" content="1200"' in body
    assert 'property="og:image:height" content="630"' in body
    assert (
        'property="og:image:alt" content="saberistic — high-stakes architecture and '
        'engineering leadership"' in body
    )
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert f'name="twitter:title" content="{page_title}"' in body
    assert f'name="twitter:description" content="{study["meta_description"]}"' in body
    assert 'name="twitter:image" content="https://saberistic.com/assets/og-share.png"' in body
    assert (
        'name="twitter:image:alt" content="saberistic — high-stakes architecture and '
        'engineering leadership"' in body
    )
    assert '"@type":"WebPage"' in body
    assert case_studies.DISCLAIMERS[study["engagement"]] in body


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
    assert "<script>" not in rendered.replace('type="application/ld+json"', "")
    assert "&lt;script&gt;" in rendered


@pytest.mark.unit
def test_render_case_studies_index_structure() -> None:
    html = case_studies.render_case_studies_index()
    assert "<title>Case studies — saberistic</title>" in html
    assert 'href="/work/brave"' in html
    assert 'href="/work/baxus"' in html
    assert 'href="/work/eternis"' in html
    assert 'href="/work/spiral-safe"' in html
    assert 'href="/work/architecture-diagnostic"' in html
    assert "Request an Architecture Diagnostic" in html
    assert 'href="/brief"' in html
    assert "proof-summary" in html
    assert '"@type": "CollectionPage"' in html


@pytest.mark.unit
def test_list_featured_slugs() -> None:
    featured = case_studies.list_featured_slugs()
    assert featured == ["brave", "baxus", "eternis"]
