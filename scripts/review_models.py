#!/usr/bin/env python3
"""GitHub Models–backed PR review against issue acceptance criteria."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from github_api import GitHubError, api, split_repo

DEFAULT_MODEL = "openai/gpt-4o-mini"
MODELS_URL = "https://models.github.ai/inference/chat/completions"


def models_token() -> str:
    value = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not value:
        raise GitHubError("missing MODELS_TOKEN for reviewer AI")
    return value


def chat(system: str, user: str, model: str | None = None) -> str:
    model = model or os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {models_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-reviewer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"Models API -> {exc.code}: {detail}") from exc
    choices = body.get("choices") or []
    if not choices:
        raise GitHubError(f"no choices: {body!r}")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise GitHubError("empty model content")
    return str(content)


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise GitHubError(f"review model did not return JSON: {text[:400]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise GitHubError("review JSON root must be object")
    return data


def collect_pr_context(repo: str, issue: int, pr_number: int) -> dict[str, Any]:
    owner, name = split_repo(repo)
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    pr = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
    files = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/files") or []
    commits = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/commits") or []
    patches = []
    for f in files[:20]:
        patch = f.get("patch") or ""
        if len(patch) > 4000:
            patch = patch[:4000] + "\n…[truncated]…"
        patches.append(
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": patch,
            }
        )
    return {
        "issue_title": issue_data.get("title"),
        "issue_body": (issue_data.get("body") or "")[:8000],
        "pr_title": pr.get("title"),
        "pr_body": (pr.get("body") or "")[:4000],
        "commit_messages": [c.get("commit", {}).get("message", "") for c in commits],
        "files": patches,
    }


def looks_like_scaffold_sync(ctx: dict[str, Any]) -> bool:
    msgs = ctx.get("commit_messages") or []
    if not msgs:
        return False
    syncish = sum(1 for m in msgs if re.search(r"\bsync\b", m, re.I))
    if syncish == len(msgs) and len(msgs) >= 2:
        return True
    body = (ctx.get("pr_body") or "").lower()
    if "brutal-minimalist landing" in body and "sync" in " ".join(msgs).lower():
        return True
    return False


def ai_review(repo: str, issue: int, pr_number: int) -> dict[str, Any]:
    ctx = collect_pr_context(repo, issue, pr_number)
    model = os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    system = (
        "You are a strict PR reviewer for an agent orchestration repo.\n"
        "Return ONLY JSON:\n"
        "{\n"
        '  "decision": "approved" | "changes-requested",\n'
        '  "meets_acceptance": boolean,\n'
        '  "reasons": ["string"],\n'
        '  "summary": "string"\n'
        "}\n"
        "Approve ONLY if the PR clearly implements the issue acceptance criteria.\n"
        "Request changes if the PR is a no-op, scaffold sync, unrelated files, "
        "or leaves acceptance criteria unmet.\n"
        "Be concrete in reasons.\n"
    )
    user = json.dumps(ctx, indent=2)
    raw = chat(system, user, model=model)
    data = extract_json(raw)
    decision = str(data.get("decision") or "").lower().replace("_", "-")
    if decision not in {"approved", "changes-requested"}:
        meets = bool(data.get("meets_acceptance"))
        decision = "approved" if meets else "changes-requested"
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    if looks_like_scaffold_sync(ctx):
        decision = "changes-requested"
        reasons = [
            "PR looks like Builder scaffold sync (sync commits / boilerplate body), "
            "not an implementation of the issue"
        ] + [str(r) for r in reasons]
    return {
        "decision": decision,
        "meets_acceptance": bool(data.get("meets_acceptance"))
        if decision == "approved"
        else False,
        "reasons": [str(r) for r in reasons][:12],
        "summary": str(data.get("summary") or ""),
        "model": model,
        "scaffold_sync": looks_like_scaffold_sync(ctx),
    }
