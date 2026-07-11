from __future__ import annotations

from acceptance import mark_body_checkboxes, parse_criteria


def test_parse_criteria_section() -> None:
    body = """## Goal
Something

## Acceptance criteria
- [ ] First thing
- [ ] Second thing with link https://example.com
- Third without box

## Out of scope
- Ignore me
"""
    items = parse_criteria(body)
    assert items == [
        "First thing",
        "Second thing with link https://example.com",
        "Third without box",
    ]


def test_mark_body_checkboxes() -> None:
    body = "## Acceptance criteria\n- [ ] First thing\n- [ ] Second thing\n"
    updated = mark_body_checkboxes(body, ["First thing"])
    assert "- [x] First thing" in updated
    assert "- [ ] Second thing" in updated
