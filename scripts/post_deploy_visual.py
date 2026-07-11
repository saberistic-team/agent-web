#!/usr/bin/env python3
"""After deploy: capture post screenshots and ask Gemini if the issue change is visible."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from github_api import GitHubError, api, post_issue_comment, split_repo
from screenshot_deploy import (
    DEFAULT_BASE,
    capture,
    comment_markdown,
    upload_to_branch,
    wait_healthy,
)


def gemini_key() -> str | None:
    value = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return value.strip() if value and value.strip() else None


def find_issue_number(message: str) -> int | None:
    # Prefer explicit closes / (#N) from PR merges
    for pattern in (
        r"(?i)(?:closes|fixes|resolves)\s+#(\d+)",
        r"\(#(\d+)\)",
        r"#(\d+)",
    ):
        m = re.search(pattern, message or "")
        if m:
            return int(m.group(1))
    return None


def list_pre_urls(repo: str, ref: str, pr: int | None) -> list[str]:
    owner, name = split_repo(repo)
    if not pr:
        return []
    prefix = f".agent/screenshots/pr-{pr}"
    try:
        nodes = api("GET", f"/repos/{owner}/{name}/contents/{prefix}?ref={ref}") or []
    except GitHubError:
        return []
    if not isinstance(nodes, list):
        return []
    urls = []
    for node in nodes:
        path = node.get("path") or ""
        if "pre-" in path and path.endswith(".png"):
            urls.append(
                f"https://raw.githubusercontent.com/{owner}/{name}/{ref}/{path}"
            )
    return urls


def gemini_visual_check(
    *,
    issue_title: str,
    issue_body: str,
    pre_paths: list[Path],
    post_paths: list[Path],
) -> dict:
    key = gemini_key()
    if not key:
        return {
            "visible": None,
            "summary": "GEMINI_API_KEY missing; skipped visual AI check",
            "decision": "skip",
        }
    model = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
    parts: list[dict] = [
        {
            "text": (
                "You compare before/after screenshots of a deployed website.\n"
                "Return ONLY JSON: "
                '{"visible": boolean, "summary": "string", "decision": "pass"|"fail"}.\n'
                "pass if the post screenshots clearly show the issue change; "
                "fail if unchanged or unrelated.\n\n"
                f"Issue title: {issue_title}\n"
                f"Issue body:\n{(issue_body or '')[:4000]}\n"
            )
        }
    ]
    for label, paths in (("BEFORE", pre_paths), ("AFTER", post_paths)):
        parts.append({"text": f"\n{label} screenshots follow."})
        for path in paths:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    }
                }
            )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model, safe='')}:generateContent"
        f"?key={urllib.parse.quote(key)}"
    )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "agent-web-visual"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"Gemini visual -> {exc.code}: {detail}") from exc
    text = ""
    for cand in body.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            text += str(part.get("text") or "")
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
    return {
        "visible": data.get("visible"),
        "summary": str(data.get("summary") or text[:500]),
        "decision": str(data.get("decision") or "fail").lower(),
        "model": model,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument(
        "--base-url", default=os.environ.get("DEPLOY_BASE_URL", DEFAULT_BASE)
    )
    args = parser.parse_args(argv)

    try:
        owner, name = split_repo(args.repo)
        issue_num = args.issue or find_issue_number(args.commit_message)
        if not issue_num:
            print("No issue number in commit message; skipping visual verify")
            return 0

        issue = api("GET", f"/repos/{owner}/{name}/issues/{issue_num}")
        default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
        wait_healthy(args.base_url)

        out = Path("trace/screenshots-post")
        post_files = capture(args.base_url, out, phase="post")
        prefix = f".agent/screenshots/issue-{issue_num}/post"
        post_urls = upload_to_branch(args.repo, default, post_files, prefix)

        # Local pre shots if present from reviewer artifact checkout; else remote URLs only
        pre_files = sorted(Path("trace/screenshots").glob("pre-*.png"))
        pre_urls = list_pre_urls(args.repo, default, args.pr) if not pre_files else []
        if pre_files:
            pre_urls = upload_to_branch(
                args.repo,
                default,
                pre_files,
                f".agent/screenshots/issue-{issue_num}/pre",
            )

        visual = gemini_visual_check(
            issue_title=issue.get("title") or "",
            issue_body=issue.get("body") or "",
            pre_paths=pre_files,
            post_paths=post_files,
        )

        body = comment_markdown(
            "### deploy_visual_check",
            args.base_url,
            post_urls,
            extra=[
                "- phase: `post-deploy`",
                f"- issue: #{issue_num}",
                f"- visual_decision: `{visual.get('decision')}`",
                f"- visual_visible: `{visual.get('visible')}`",
                f"- visual_model: `{visual.get('model')}`",
                f"- visual_summary: {visual.get('summary')}",
            ],
        )
        if pre_urls:
            body += "\n#### Pre-merge screenshots\n" + "\n".join(
                f"- {u.rsplit('/', 1)[-1]}: ![]({u})" for u in pre_urls
            ) + "\n"
        post_issue_comment(args.repo, issue_num, body)

        if visual.get("decision") == "fail":
            post_issue_comment(
                args.repo,
                issue_num,
                "@human-review Post-deploy visual check failed — change may not be visible on production.\n",
            )
            print("visual check failed", file=sys.stderr)
            return 1
        print(json.dumps({"issue": issue_num, "visual": visual, "post": post_urls}))
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
