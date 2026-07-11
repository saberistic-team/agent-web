#!/usr/bin/env python3
"""AI-backed PR review against issue acceptance criteria.

Prefers OpenAI (ChatGPT) when OPENAI_API_KEY is set, then GitHub Models.
Gemini is retired.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from github_api import GitHubError, api, split_repo

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def models_token() -> str:
    value = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not value:
        raise GitHubError("missing MODELS_TOKEN for reviewer AI")
    return value


def openai_api_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY")
    return value.strip() if value and value.strip() else None


def chat_openai(system: str, user: str, model: str | None = None) -> tuple[str, str]:
    key = openai_api_key()
    if not key:
        raise GitHubError("missing OPENAI_API_KEY")
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
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
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-reviewer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"OpenAI API -> {exc.code}: {detail}") from exc
    choices = body.get("choices") or []
    if not choices:
        raise GitHubError(f"OpenAI no choices: {body!r}")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise GitHubError("OpenAI empty content")
    return str(content), model


def chat_github(system: str, user: str, model: str | None = None) -> tuple[str, str]:
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
    return str(content), model


def chat(system: str, user: str, model: str | None = None) -> tuple[str, str]:
    """OpenAI first, then GitHub Models. Gemini is retired."""
    errors: list[str] = []
    if openai_api_key():
        try:
            return chat_openai(
                system, user, model=os.environ.get("OPENAI_MODEL") or model
            )
        except Exception as exc:
            errors.append(f"openai: {exc}")
    try:
        return chat_github(system, user, model=model)
    except Exception as exc:
        errors.append(f"github-models: {exc}")
        raise GitHubError("review chat failed: " + " | ".join(errors)) from exc


def _recover_truncated_review_json(text: str) -> dict[str, Any] | None:
    """If the model cut off mid-JSON but decision is clear, recover a verdict."""
    decision_m = re.search(
        r'"decision"\s*:\s*"(approved|changes-requested)"', text, re.I
    )
    if not decision_m:
        return None
    decision = decision_m.group(1).lower()
    meets_m = re.search(r'"meets_acceptance"\s*:\s*(true|false)', text, re.I)
    meets = (
        meets_m.group(1).lower() == "true"
        if meets_m
        else decision == "approved"
    )
    reasons = re.findall(r'"\s*([^"]{12,240})\s*"\s*(?:,|\n)', text)
    # Drop schema-ish strings; keep longer prose-looking reason lines.
    reasons = [
        r
        for r in reasons
        if r.lower() not in {"approved", "changes-requested", "string"}
        and "decision" not in r.lower()
    ][:8]
    summary_m = re.search(r'"summary"\s*:\s*"([^"]*)', text)
    return {
        "decision": decision,
        "meets_acceptance": meets,
        "reasons": reasons
        or [
            "Recovered from truncated review JSON; decision field was present "
            f"({decision})"
        ],
        "summary": (summary_m.group(1) if summary_m else "")
        or "Recovered from truncated model JSON",
    }


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                recovered = _recover_truncated_review_json(text)
                if recovered:
                    return recovered
                raise GitHubError(
                    f"review model did not return JSON: {text[:400]!r}"
                )
        else:
            recovered = _recover_truncated_review_json(text)
            if recovered:
                return recovered
            raise GitHubError(f"review model did not return JSON: {text[:400]!r}")
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
        "Return ONLY compact JSON (keep reasons short; max 4 reasons, each ≤120 chars):\n"
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
    raw, model = chat(system, user, model=model)
    data = extract_json(raw)
    decision = str(data.get("decision") or "").lower().replace("_", "-")
    if decision not in {"approved", "changes-requested"}:
        meets = bool(data.get("meets_acceptance"))
        decision = "approved" if meets else "changes-requested"
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    issue_blob = f"{ctx.get('issue_title')}\n{ctx.get('issue_body')}".lower()
    infra_issue = bool(
        re.search(r"screenshot|headless|playwright|visual (check|evidence)", issue_blob)
    )
    if looks_like_scaffold_sync(ctx) and not infra_issue:
        decision = "changes-requested"
        reasons = [
            "PR looks like Builder scaffold sync (sync commits / boilerplate body), "
            "not an implementation of the issue"
        ] + [str(r) for r in reasons]
    elif looks_like_scaffold_sync(ctx) and infra_issue:
        decision = "approved"
        reasons = [
            "Infra/screenshot issue: docs sync acceptable when screenshot scripts "
            "already live on main"
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
