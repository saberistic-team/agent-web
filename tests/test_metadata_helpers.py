"""Unit tests for shared metadata helpers."""

from __future__ import annotations

import pytest

from app.metadata import (
    OG_IMAGE_ALT,
    article_json_ld,
    case_study_page_head_tags,
    case_study_web_page_json_ld,
    json_ld_script,
    social_meta_tags,
)


@pytest.mark.unit
def test_social_meta_tags_include_og_and_twitter() -> None:
    tags = social_meta_tags(
        title="Test title",
        description="Test description",
        url="https://saberistic.com/insights/test",
        og_type="article",
    )
    assert 'property="og:type" content="article"' in tags
    assert 'name="twitter:card" content="summary_large_image"' in tags
    assert "Test title" in tags
    assert OG_IMAGE_ALT in tags


@pytest.mark.unit
def test_json_ld_script_escapes_closing_script_sequence() -> None:
    script = json_ld_script({"@context": "https://schema.org", "headline": "</script>evil"})
    assert "<\\/script>" in script
    assert "</script>evil" not in script


@pytest.mark.unit
def test_article_json_ld_includes_modified_date() -> None:
    script = article_json_ld(
        title="Title",
        description="Description",
        url="https://saberistic.com/insights/t",
        author="AmirSaber Sharifi",
        date_published="2026-07-01",
        date_modified="2026-07-13",
    )
    assert '"dateModified":"2026-07-13"' in script


@pytest.mark.unit
def test_case_study_web_page_json_ld_includes_is_part_of() -> None:
    script = case_study_web_page_json_ld(
        title="Brave — Infrastructure · saberistic",
        description="Prior employer role.",
        url="https://saberistic.com/work/brave",
    )
    assert '"@type":"WebPage"' in script
    assert '"@type":"ProfessionalService"' in script
    assert '"isPartOf"' in script


@pytest.mark.unit
def test_case_study_page_head_tags_include_canonical_and_social() -> None:
    tags = case_study_page_head_tags(
        title="Brave — Infrastructure · saberistic",
        description="Prior employer role.",
        canonical_path="/work/brave",
    )
    assert 'rel="canonical" href="https://saberistic.com/work/brave"' in tags
    assert 'property="og:url" content="https://saberistic.com/work/brave"' in tags
    assert 'name="twitter:card" content="summary_large_image"' in tags
    assert '"@type":"WebPage"' in tags
