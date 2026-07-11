from __future__ import annotations

from review_models import extract_json


def test_extract_json_recovers_truncated_approved() -> None:
    raw = (
        '{\n  "decision": "approved",\n  "meets_acceptance": true,\n'
        '  "reasons": [\n'
        '    "The dedicated About page is successfully implemented at `/about`.",\n'
        '    "The exact copy requested is fully preserved in `site/about.html`",\n'
    )
    data = extract_json(raw)
    assert data["decision"] == "approved"
    assert data["meets_acceptance"] is True


def test_extract_json_full_object() -> None:
    raw = (
        '{"decision":"changes-requested","meets_acceptance":false,'
        '"reasons":["missing tests"],"summary":"no"}'
    )
    data = extract_json(raw)
    assert data["decision"] == "changes-requested"
    assert data["meets_acceptance"] is False
