"""Unit tests for shared metadata helpers."""

from __future__ import annotations

import pytest

from app.metadata import (
    OG_IMAGE,
    OG_IMAGE_ALT,
    article_json_ld,
    case_study_head_metadata,
    case_study_json_ld,
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
def test_case_study_json_ld_escapes_angle_brackets() -> None:
    script = case_study_json_ld(
        title="Org<script>",
        description="Meta<script>",
        url="https://saberistic.com/work/xss",
    )
    assert "<script>" not in script
    assert "\\u003c" in script
    script = case_study_json_ld(
        title="Brave — Infrastructure · saberistic",
        description="Case study description",
        url="https://saberistic.com/work/brave",
    )
    assert '"@type":"WebPage"' in script
    assert OG_IMAGE in script


@pytest.mark.unit
def test_case_study_head_metadata_includes_social_and_json_ld() -> None:
    block = case_study_head_metadata(
        title="Brave — Infrastructure · saberistic",
        description="Case study description",
        url="https://saberistic.com/work/brave",
    )
    assert 'property="og:type" content="website"' in block
    assert 'name="twitter:card" content="summary_large_image"' in block
    assert '"@type":"WebPage"' in block
    assert OG_IMAGE_ALT in block


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
