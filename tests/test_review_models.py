from __future__ import annotations

from unittest.mock import patch

from review_models import ai_review, extract_json


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


def test_ai_review_approves_when_only_admin_screenshot_reasons() -> None:
    """Do not block on missing /admin PNGs or saberistic.com pre shots."""
    fake_ctx = {
        "issue_title": "Admin shell layout",
        "issue_body": "## Acceptance criteria\n\n- [ ] Desktop and mobile /admin screenshots\n",
        "pr_title": "builder: admin shell",
        "pr_body": "Closes #132",
        "commit_messages": ["builder(#132): implement admin shell"],
        "files": [{"path": "app/admin_pages.py", "patch": "+layout"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":false,'
        '"reasons":["Missing branch-admin.png and branch-admin-mobile.png",'
        '"acceptance requires desktop and mobile /admin review screenshots"],'
        '"summary":"need admin shots"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 132, 140)
    assert verdict["decision"] == "approved"
    assert verdict["meets_acceptance"] is True


def test_ai_review_approves_when_acceptance_met_despite_screenshot_nits() -> None:
    """#58: AI requested changes for .agent/screenshots + history while AC met."""
    fake_ctx = {
        "issue_title": "Brief form emails",
        "issue_body": "## Acceptance criteria\n\n- [ ] Email on submit\n",
        "pr_title": "builder: implement",
        "pr_body": "Closes #58",
        "commit_messages": ["builder(#58): implement", "review: record pre-home.png"],
        "files": [{"path": "app/main.py", "patch": "+notify"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":true,'
        '"reasons":["Adds unrelated .agent/screenshots/pr-60/pre-home.png",'
        '"Commit history should be squashed"],'
        '"summary":"AC met but nits"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 58, 60)
    assert verdict["decision"] == "approved"
    assert verdict["meets_acceptance"] is True


def test_ai_review_keeps_changes_when_real_reason_and_acceptance_claimed() -> None:
    fake_ctx = {
        "issue_title": "Brief form emails",
        "issue_body": "## Acceptance criteria\n\n- [ ] Email on submit\n",
        "pr_title": "builder: implement",
        "pr_body": "Closes #58",
        "commit_messages": ["builder(#58): implement"],
        "files": [{"path": "app/main.py", "patch": "+notify"}],
    }
    raw = (
        '{"decision":"changes-requested","meets_acceptance":true,'
        '"reasons":["Lead emails only run after Stripe succeeds; 502 skips inbox"],'
        '"summary":"real gap"}'
    )
    with patch("review_models.collect_pr_context", return_value=fake_ctx):
        with patch("review_models.chat", return_value=(raw, "test-model")):
            verdict = ai_review("o/r", 58, 60)
    assert verdict["decision"] == "changes-requested"
    assert verdict["meets_acceptance"] is False
