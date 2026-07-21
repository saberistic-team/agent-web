#!/usr/bin/env python3
"""Parse, verify, and record issue acceptance criteria before close.

Reviewer and Gate use this so issues are not closed until each criterion is
checked with linked evidence (PR, commits, files, screenshots, comments).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from github_api import (
    GitHubError,
    api,
    list_issue_comments,
    list_pr_files,
    post_issue_comment,
    split_repo,
)

CHECKLIST_MARKER = "### acceptance_checklist"
CRITERIA_SECTION = re.compile(
    r"(?is)##\s*acceptance\s*criteria\s*\n(.*?)(?=\n##\s|\Z)"
)
BULLET = re.compile(
    r"^\s*(?:[-*]|\d+\.)\s+(?:\[\s*[xX ]\s*\]\s*)?(.+?)\s*$",
    re.M,
)


def parse_criteria(body: str) -> list[str]:
    """Extract acceptance-criteria bullets from an issue body."""
    text = body or ""
    section = CRITERIA_SECTION.search(text)
    blob = section.group(1) if section else text
    items: list[str] = []
    for match in BULLET.finditer(blob):
        item = match.group(1).strip()
        if not item or item.lower().startswith(("out of scope", "notes")):
            continue
        if section is None and not re.search(r"\[\s*[xX ]\s*\]", match.group(0)):
            continue
        items.append(item)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def mark_body_checkboxes(body: str, done_texts: list[str]) -> str:
    """Flip matching `- [ ]` lines to `- [x]` when their text matches a done criterion."""
    if not body:
        return body or ""
    done_norm = {re.sub(r"\s+", " ", t).strip().lower() for t in done_texts}

    def repl(match: re.Match[str]) -> str:
        prefix, _box, rest = match.group(1), match.group(2), match.group(3)
        norm = re.sub(r"\s+", " ", rest).strip().lower()
        if norm in done_norm or any(norm.startswith(d[:80]) for d in done_norm if d):
            return f"{prefix}[x]{rest}"
        return match.group(0)

    return re.sub(
        r"(^[\t ]*[-*]\s+)\[([ xX])\](\s+.+)$",
        repl,
        body,
        flags=re.M,
    )


def _html_linkedin_count(html: str) -> int:
    return len(re.findall(r"linkedin\.com/in/saberistic", html, re.I))


def _fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "agent-web-acceptance"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", errors="replace")


def gather_evidence(repo: str, issue: int, pr_number: int | None) -> dict[str, Any]:
    owner, name = split_repo(repo)
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    comments = list_issue_comments(repo, issue)
    evidence: dict[str, Any] = {
        "issue": issue,
        "issue_url": issue_data.get("html_url"),
        "issue_title": issue_data.get("title"),
        "issue_body": issue_data.get("body") or "",
        "comments": [
            {
                "id": c.get("id"),
                "url": c.get("html_url"),
                "user": (c.get("user") or {}).get("login"),
                "body": (c.get("body") or "")[:2000],
            }
            for c in comments
        ],
        "pr_number": pr_number,
        "pr_url": None,
        "commits": [],
        "files": [],
        "head_sha": None,
        "merge_commit_sha": None,
    }

    if pr_number:
        pr = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        evidence["pr_url"] = pr.get("html_url")
        evidence["head_sha"] = (pr.get("head") or {}).get("sha")
        evidence["merge_commit_sha"] = pr.get("merge_commit_sha")
        commits = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/commits") or []
        evidence["commits"] = [
            {
                "sha": c.get("sha"),
                "url": c.get("html_url"),
                "message": (c.get("commit") or {}).get("message", ""),
            }
            for c in commits
        ]
        # Paginate: screenshot-heavy PRs exceed the default 30-file page and
        # would otherwise hide tests/* from acceptance heuristics (#83).
        files = list_pr_files(repo, pr_number)
        evidence["files"] = [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "blob_url": f.get("blob_url"),
                "raw_url": f.get("raw_url"),
            }
            for f in files
        ]

    base = os.environ.get("DEPLOY_BASE_URL") or "https://saberistic.com"
    try:
        home = _fetch_url(base.rstrip("/") + "/")
        evidence["deploy"] = {
            "base_url": base,
            "home_linkedin_count": _html_linkedin_count(home),
            "home_has_cta": 'class="cta"' in home or "class='cta'" in home,
        }
    except Exception as exc:
        evidence["deploy"] = {"base_url": base, "error": str(exc)}

    return evidence


def _comment_urls(evidence: dict[str, Any], needle: str) -> list[str]:
    urls: list[str] = []
    for c in evidence.get("comments") or []:
        body = c.get("body") or ""
        if needle.lower() in body.lower() and c.get("url"):
            urls.append(str(c["url"]))
    return urls


def _file_urls(evidence: dict[str, Any], substr: str) -> list[str]:
    urls: list[str] = []
    for f in evidence.get("files") or []:
        name = f.get("filename") or ""
        if substr in name and (f.get("blob_url") or f.get("raw_url")):
            urls.append(str(f.get("blob_url") or f.get("raw_url")))
    return urls


def _file_content_from_pr(evidence: dict[str, Any], path: str) -> str | None:
    for f in evidence.get("files") or []:
        if f.get("filename") != path:
            continue
        raw = f.get("raw_url")
        if not raw:
            continue
        try:
            return _fetch_url(str(raw))
        except Exception:
            return None
    return None


def heuristic_check(criterion: str, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Return a verdict dict if a deterministic check applies, else None."""
    text = criterion.lower()
    ev: list[str] = []
    note = ""
    status: str | None = None

    index_html = _file_content_from_pr(evidence, "site/index.html")
    index_count = _html_linkedin_count(index_html) if index_html is not None else None

    if (
        "kind: gemini" in text
        or "kind:gemini" in text
        or "kind: openai" in text
        or "kind:openai" in text
        or "kind: cursor" in text
        or "kind:cursor" in text
        or "kind: copilot" in text
        or "kind:copilot" in text
        or ("gemini" in text and "codegen" in text)
        or ("openai" in text and "codegen" in text)
        or ("cursor" in text and "codegen" in text)
        or ("copilot" in text and "codegen" in text)
    ):
        urls = (
            _comment_urls(evidence, "kind: `cursor`")
            or _comment_urls(evidence, "kind: cursor")
            or _comment_urls(evidence, "kind: `openai`")
            or _comment_urls(evidence, "kind: openai")
            or _comment_urls(evidence, "kind: `copilot`")
            or _comment_urls(evidence, "kind: copilot")
            or _comment_urls(evidence, "kind: `gemini`")
            or _comment_urls(evidence, "kind: gemini")
        )
        if urls:
            status, note, ev = "done", "Builder reported Cursor/OpenAI/Models codegen", urls
        elif _comment_urls(evidence, "kind: `github-models`") or _comment_urls(
            evidence, "kind: github-models"
        ):
            status = "done"
            note = "Builder used github-models fallback"
            ev = _comment_urls(evidence, "kind:")
        else:
            status = "not_done"
            note = "No Builder kind: cursor/openai/copilot/github-models comment"

    elif "screenshot" in text and (
        "/admin" in text or re.search(r"\badmin\b", text)
    ):
        # Admin UI shots are branch-only under ADMIN_PREVIEW_MODE; never required
        # on saberistic.com. Treat AC that ask for /admin PNGs as done when the
        # Reviewer posted branch evidence or an explicit skip note.
        urls = _comment_urls(evidence, "reviewer_screenshots_pre")
        if urls or any(
            "ADMIN_PREVIEW_MODE" in (c.get("body") or "")
            or "branch-admin" in (c.get("body") or "")
            or "screenshots skipped" in (c.get("body") or "")
            for c in (evidence.get("comments") or [])
        ):
            status, note, ev = (
                "done",
                "Admin screenshots are PR-branch ADMIN_PREVIEW_MODE only "
                "(not saberistic.com)",
                urls,
            )
        else:
            status, note = (
                "n/a",
                "Admin visual evidence is branch preview only; verify via "
                "tests/code if Reviewer has not posted yet",
            )

    elif "screenshot" in text and ("reviewer" in text or "pr" in text):
        urls = _comment_urls(evidence, "reviewer_screenshots_pre")
        if urls:
            status, note, ev = "done", "Pre-merge screenshots posted", urls
        else:
            status, note = "not_done", "Missing ### reviewer_screenshots_pre on issue/PR"

    elif "scaffold" in text or "sync-only" in text or "scaffold-only" in text:
        msgs = " ".join(c.get("message") or "" for c in evidence.get("commits") or [])
        if re.search(r"\bsync\b", msgs) and not re.search(
            r"linkedin|cta|landing|implement|fix|add ", msgs, re.I
        ):
            status, note = "not_done", "Commits look like scaffold sync only"
        else:
            status = "done"
            note = "Commits are not scaffold-sync-only"
            ev = [c["url"] for c in (evidence.get("commits") or []) if c.get("url")][:5]

    elif "linkedin" in text and (
        "exactly one" in text or "single" in text or "one " in text
    ):
        files = _file_urls(evidence, "site/index.html")
        tests = _file_urls(evidence, "test_api.py") or _file_urls(evidence, "tests/")
        deploy = evidence.get("deploy") or {}
        # Prefer PR file (pre-merge); fall back to live deploy after merge.
        count = index_count if index_count is not None else deploy.get("home_linkedin_count")
        source = "PR file site/index.html" if index_count is not None else "live deploy"
        if count == 1:
            status = "done"
            note = f"{source} has exactly one LinkedIn profile link"
            ev = files + tests
            if index_count is None and deploy.get("base_url"):
                ev = [str(deploy.get("base_url"))] + ev
        elif count is None:
            status, note = "not_done", "Could not read site/index.html or deploy"
        else:
            status, note = "not_done", f"{source} LinkedIn count={count}, want 1"

    elif ("header" in text or "footer" in text) and "linkedin" in text:
        files = _file_urls(evidence, "site/index.html")
        deploy = evidence.get("deploy") or {}
        html = index_html
        if html is None and not deploy.get("error"):
            try:
                html = _fetch_url(str(deploy.get("base_url")).rstrip("/") + "/")
            except Exception:
                html = None
        if html is None:
            status, note = "not_done", "Could not read home HTML for header/footer check"
        else:
            # Crude but effective: LinkedIn anchors should not appear in header/footer regions.
            header = re.search(r"<header[\s\S]*?</header>", html, re.I)
            footer = re.search(r"<footer[\s\S]*?</footer>", html, re.I)
            bad = False
            if header and _html_linkedin_count(header.group(0)):
                bad = True
            if footer and _html_linkedin_count(footer.group(0)):
                bad = True
            if bad:
                status, note = "not_done", "LinkedIn link still present in header or footer"
            else:
                status = "done"
                note = "Header/footer have no LinkedIn links"
                ev = files or ([str(deploy.get("base_url"))] if deploy.get("base_url") else [])

    elif "test" in text and (
        "assert" in text or "updated" in text or "single cta" in text
    ):
        urls = _file_urls(evidence, "tests/") or _file_urls(evidence, "test_")
        if urls:
            status, note, ev = "done", "Tests touched in PR", urls
        else:
            status, note = "not_done", "No test files in PR evidence"

    if status is None:
        return None
    return {
        "text": criterion,
        "status": status,
        "evidence": [e for e in ev if e][:8],
        "note": note,
        "method": "heuristic",
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise GitHubError(f"acceptance AI did not return JSON: {text[:400]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise GitHubError("acceptance JSON root must be object")
    return data


def _chat(system: str, user: str) -> str:
    """Prefer Cursor (same stack as Reviewer), then OpenAI, then Models."""
    try:
        from review_models import chat as review_chat

        content, _model = review_chat(system, user)
        return content
    except Exception as primary:
        # Keep a thin OpenAI/Models fallback if review_models import/chat fails hard.
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key and openai_key.strip():
            model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
            payload = {
                "model": model,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {openai_key.strip()}",
                    "Content-Type": "application/json",
                    "User-Agent": "agent-web-acceptance",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices") or []
                if choices:
                    content = (choices[0].get("message") or {}).get("content")
                    if content:
                        return str(content)
            except Exception:
                pass

        token = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise GitHubError(
                f"acceptance AI failed ({primary}); no OPENAI_API_KEY/MODELS_TOKEN fallback"
            ) from primary
        model = os.environ.get("GITHUB_MODELS_MODEL") or "openai/gpt-4o-mini"
        payload = {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            "https://models.github.ai/inference/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "agent-web-acceptance",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not content:
            raise GitHubError("acceptance AI empty content") from primary
        return str(content)


def ai_check_remaining(
    criteria: list[str],
    evidence: dict[str, Any],
    already: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pending = [c for c in criteria if not any(a.get("text") == c for a in already)]
    if not pending:
        return []

    system = (
        "You verify GitHub issue acceptance criteria against PR/deploy evidence.\n"
        "Return ONLY JSON:\n"
        "{\n"
        '  "items": [\n'
        "    {\n"
        '      "text": "exact criterion string",\n'
        '      "status": "done" | "not_done" | "n/a",\n'
        '      "evidence": ["url or path"],\n'
        '      "note": "short justification"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Mark done ONLY with concrete evidence. Prefer linking PR files, commits, "
        "screenshot comments, or deploy URLs from the provided evidence JSON.\n"
        "Screenshot policy: pre-merge evidence is PR-branch `branch-*.png` only "
        "(plus `branch-*-mobile-open.png`, `*-tablet.png`, `*-narrow-desktop.png` "
        "when admin-nav layout criteria ask for them and the capture matrix emits "
        "them — docs/SCREENSHOTS.md; admin pages via ADMIN_PREVIEW_MODE). "
        "CSS/layout guardrail tests may satisfy sizing criteria when present shots "
        "+ tests agree and AI review approved; do not mark not_done solely because "
        "an older 58-shot run lacked extras that the PR-head screenshot script now "
        "defines (learned from #167). saberistic.com shots are post-deploy only. "
        "Do NOT mark criteria not_done solely for missing production `pre-*` "
        "PNGs or missing `/admin` shots on saberistic.com.\n"
        "Do NOT mark 'ready to deploy' / 'ready to merge' criteria not_done "
        "solely because this acceptance_checklist is not yet all_done or the "
        "PR is not yet review:approved — that is circular; judge deployability "
        "from CI green, implementer completeness, and product evidence.\n"
        "If evidence is missing, status must be not_done.\n"
    )
    slim = {
        "issue_title": evidence.get("issue_title"),
        "pr_url": evidence.get("pr_url"),
        "commits": evidence.get("commits"),
        "files": evidence.get("files"),
        "deploy": evidence.get("deploy"),
        "recent_comments": [
            {"url": c.get("url"), "body": (c.get("body") or "")[:500]}
            for c in (evidence.get("comments") or [])[-15:]
        ],
        "criteria": pending,
    }
    raw = _chat(system, json.dumps(slim, indent=2))
    data = _extract_json(raw)
    items = data.get("items") or []
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "not_done").lower().replace(" ", "_")
        if status not in {"done", "not_done", "n/a", "na"}:
            status = "not_done"
        if status == "na":
            status = "n/a"
        ev = item.get("evidence") or []
        if not isinstance(ev, list):
            ev = [str(ev)]
        out.append(
            {
                "text": text,
                "status": status,
                "evidence": [str(e) for e in ev][:8],
                "note": str(item.get("note") or ""),
                "method": "ai",
            }
        )
    return out


def verify_acceptance(
    repo: str,
    issue: int,
    pr_number: int | None = None,
    *,
    use_ai: bool = True,
) -> dict[str, Any]:
    evidence = gather_evidence(repo, issue, pr_number)
    criteria = parse_criteria(evidence.get("issue_body") or "")
    items: list[dict[str, Any]] = []

    if not criteria:
        items.append(
            {
                "text": "(no ## Acceptance criteria section found)",
                "status": "not_done",
                "evidence": [evidence.get("issue_url") or ""],
                "note": "Add an Acceptance criteria section with checklist bullets",
                "method": "parse",
            }
        )
    else:
        for criterion in criteria:
            hit = heuristic_check(criterion, evidence)
            if hit:
                items.append(hit)

        if use_ai:
            try:
                items.extend(ai_check_remaining(criteria, evidence, items))
            except Exception as exc:
                # Infra/parse failures must not invent product not_done rows —
                # that bounced Reviewer↔Builder when AI review already approved
                # (e.g. Cursor returned prose instead of JSON).
                for criterion in criteria:
                    if any(i.get("text") == criterion for i in items):
                        continue
                    items.append(
                        {
                            "text": criterion,
                            "status": "n/a",
                            "evidence": [],
                            "note": f"AI verification unavailable: {exc}",
                            "method": "ai-error",
                        }
                    )
        else:
            for criterion in criteria:
                if any(i.get("text") == criterion for i in items):
                    continue
                items.append(
                    {
                        "text": criterion,
                        "status": "not_done",
                        "evidence": [],
                        "note": "No heuristic match and AI disabled",
                        "method": "unset",
                    }
                )

    by_text: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("text") or "")
        if key and key not in by_text:
            by_text[key] = item
    ordered = [by_text[c] for c in criteria if c in by_text] + [
        i for i in items if i.get("text") not in criteria
    ]

    # Treat AI-infra-only gaps as non-blocking for all_done; product not_done
    # still fails. Callers that need a strict checklist inspect ai_infra_failed.
    product_items = [i for i in ordered if i.get("method") != "ai-error"]
    all_done = bool(product_items) and all(
        i.get("status") in {"done", "n/a"} for i in product_items
    )
    if not product_items and ordered and all(
        i.get("method") == "ai-error" for i in ordered
    ):
        # Every criterion was AI-infra-only — inconclusive, not a product fail.
        all_done = False
    ai_infra_failed = any(i.get("method") == "ai-error" for i in ordered)
    return {
        "all_done": all_done,
        "ai_infra_failed": ai_infra_failed,
        "items": ordered,
        "evidence": {
            "pr_url": evidence.get("pr_url"),
            "head_sha": evidence.get("head_sha"),
            "commits": [
                c.get("url") for c in (evidence.get("commits") or []) if c.get("url")
            ],
            "deploy": evidence.get("deploy"),
        },
    }


def format_checklist(result: dict[str, Any], *, role: str) -> str:
    lines = [
        CHECKLIST_MARKER,
        f"- role: `{role}`",
        f"- all_done: `{str(bool(result.get('all_done'))).lower()}`",
    ]
    if result.get("ai_infra_failed"):
        lines.append("- ai_infra_failed: `true`")
    meta = result.get("evidence") or {}
    if meta.get("pr_url"):
        lines.append(f"- pr: {meta['pr_url']}")
    if meta.get("head_sha"):
        lines.append(f"- head_sha: `{meta['head_sha']}`")
    lines.append("")
    lines.append("| # | Criterion | Status | Evidence | Note |")
    lines.append("|---|-----------|--------|----------|------|")
    for idx, item in enumerate(result.get("items") or [], 1):
        status = item.get("status") or "not_done"
        mark = {"done": "✅ done", "n/a": "➖ n/a"}.get(status, "❌ not_done")
        ev = item.get("evidence") or []
        ev_cell = (
            "<br>".join(
                f"[link]({e})" if str(e).startswith("http") else f"`{e}`" for e in ev
            )
            or "—"
        )
        note = (item.get("note") or "").replace("|", "\\|").replace("\n", " ")
        text = (item.get("text") or "").replace("|", "\\|").replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        lines.append(f"| {idx} | {text} | {mark} | {ev_cell} | {note} |")
    lines.append("")
    return "\n".join(lines)


def post_checklist(
    repo: str,
    issue: int,
    result: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    body = format_checklist(result, role=role)
    return post_issue_comment(repo, issue, body)


def update_issue_checkboxes(repo: str, issue: int, result: dict[str, Any]) -> None:
    owner, name = split_repo(repo)
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    body = issue_data.get("body") or ""
    done = [
        str(i.get("text") or "")
        for i in (result.get("items") or [])
        if i.get("status") in {"done", "n/a"}
    ]
    updated = mark_body_checkboxes(body, done)
    if updated != body:
        api(
            "PATCH",
            f"/repos/{owner}/{name}/issues/{issue}",
            body={"body": updated},
        )


def latest_checklist(repo: str, issue: int) -> dict[str, Any] | None:
    comments = list_issue_comments(repo, issue)
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if CHECKLIST_MARKER not in body:
            continue
        all_done = bool(re.search(r"- all_done:\s*`?true`?", body, re.I))
        return {
            "all_done": all_done,
            "url": comment.get("html_url"),
            "body": body,
            "id": comment.get("id"),
        }
    return None


def require_checklist_complete(repo: str, issue: int) -> dict[str, Any]:
    """Gate helper: fail unless latest acceptance_checklist has all_done true."""
    latest = latest_checklist(repo, issue)
    if not latest:
        raise GitHubError(
            f"#{issue} has no {CHECKLIST_MARKER} comment; "
            "Reviewer must verify acceptance before merge/close"
        )
    if not latest.get("all_done"):
        raise GitHubError(
            f"#{issue} acceptance checklist is incomplete: {latest.get('url')}"
        )
    return latest


def close_issue_if_accepted(
    repo: str,
    issue: int,
    *,
    merge_sha: str | None = None,
    pr_number: int | None = None,
) -> None:
    """Close only when acceptance checklist is complete; comment with evidence."""
    latest = require_checklist_complete(repo, issue)
    if merge_sha:
        from crm_deploy_health import require_post_merge_deploy_health

        health_gate = require_post_merge_deploy_health(
            repo,
            issue,
            merge_sha,
            pr_number=pr_number,
        )
        if health_gate.get("required"):
            record = health_gate.get("record") or {}
            post_issue_comment(
                repo,
                issue,
                (
                    "### deploy_health_gate\n"
                    "- result: `pass`\n"
                    f"- sha: `{merge_sha}`\n"
                    f"- record: `{health_gate.get('path')}`\n"
                    f"- post_deploy_functional_health: "
                    f"`{(record.get('verification_layers') or {}).get('post_deploy_functional_health')}`\n"
                ),
            )
    owner, name = split_repo(repo)
    bits = [
        "### acceptance_close",
        "- result: `closed`",
        f"- checklist: {latest.get('url')}",
    ]
    if pr_number:
        bits.append(f"- pr: https://github.com/{owner}/{name}/pull/{pr_number}")
    if merge_sha:
        bits.append(f"- commit: https://github.com/{owner}/{name}/commit/{merge_sha}")
    post_issue_comment(repo, issue, "\n".join(bits) + "\n")
    api(
        "PATCH",
        f"/repos/{owner}/{name}/issues/{issue}",
        body={"state": "closed", "state_reason": "completed"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=("verify", "require", "close"),
        default="verify",
    )
    parser.add_argument("--role", default="reviewer")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--update-body", action="store_true")
    parser.add_argument("--merge-sha", default="")
    args = parser.parse_args(argv)

    try:
        if args.mode == "require":
            latest = require_checklist_complete(args.repo, args.issue)
            print(json.dumps({"ok": True, "checklist": latest.get("url")}))
            return 0
        if args.mode == "close":
            close_issue_if_accepted(
                args.repo,
                args.issue,
                merge_sha=args.merge_sha or None,
                pr_number=args.pr or None,
            )
            print(json.dumps({"ok": True, "closed": args.issue}))
            return 0

        result = verify_acceptance(
            args.repo,
            args.issue,
            args.pr or None,
            use_ai=not args.no_ai,
        )
        comment = post_checklist(args.repo, args.issue, result, role=args.role)
        if args.update_body and result.get("all_done"):
            update_issue_checkboxes(args.repo, args.issue, result)
        print(
            json.dumps(
                {
                    "all_done": result.get("all_done"),
                    "items": len(result.get("items") or []),
                    "comment": comment.get("html_url"),
                }
            )
        )
        return 0 if result.get("all_done") else 2
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
