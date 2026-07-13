"""Unit tests for shared metadata helpers."""

from __future__ import annotations

import pytest

from app.metadata import (
    OG_IMAGE_ALT,
    article_json_ld,
    case_study_head_extras,
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
def test_case_study_head_extras_include_og_twitter_and_webpage_json_ld() -> None:
    extras = case_study_head_extras(
        title="Brave — Infrastructure for privacy-aligned payments · saberistic",
        description="How infrastructure architecture supported privacy-sensitive payment systems at Brave.",
        url="https://saberistic.com/work/brave",
    )
    assert 'property="og:type" content="website"' in extras
    assert 'name="twitter:card" content="summary_large_image"' in extras
    assert '"@type":"WebPage"' in extras
    assert OG_IMAGE_ALT in extras
