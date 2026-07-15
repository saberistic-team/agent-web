#!/usr/bin/env python3
"""AI-backed PR review against issue acceptance criteria.

Prefers Cursor Agent SDK when CURSOR_API_KEY is set, then OpenAI, then
GitHub Models. Gemini is retired.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from cursor_model import DEFAULT_CURSOR_MODEL, cursor_model_dict, cursor_model_selection
from github_api import GitHubError, api, split_repo

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
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


def cursor_api_key() -> str | None:
    value = os.environ.get("CURSOR_API_KEY")
    return value.strip() if value and value.strip() else None


def chat_cursor(system: str, user: str, model: str | None = None) -> tuple[str, str]:
    """Ask-only Cursor agent turn; must not modify the workspace."""
    key = cursor_api_key()
    if not key:
        raise GitHubError("missing CURSOR_API_KEY")
    model = model or os.environ.get("CURSOR_MODEL") or DEFAULT_CURSOR_MODEL
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
        from cursor_sdk_patch import patch_callback_auth_tokens

        patch_callback_auth_tokens()
    except ImportError as exc:
        raise GitHubError(
            "cursor-sdk is not installed; pip install -r requirements-agents.txt"
        ) from exc

    prompt = (
        "READ-ONLY REVIEW TASK. Do not create, edit, delete, or move any files. "
        "Do not run mutating shell/git commands. Do not open PRs.\n"
        "Respond with JSON only (no prose outside JSON).\n\n"
        f"## Instructions\n{system}\n\n"
        f"## Context\n{user}\n"
    )
    try:
        result = Agent.prompt(
            prompt,
            AgentOptions(
                model=cursor_model_selection(model),
                api_key=key,
                name="reviewer-ai",
                mode="plan",
                local=LocalAgentOptions(cwd=os.getcwd()),
            ),
        )
    except TypeError:
        # Older SDK builds may reject mode= / AgentOptions kwargs shape.
        try:
            result = Agent.prompt(
                prompt,
                {
                    "model": cursor_model_dict(model),
                    "apiKey": key,
                    "name": "reviewer-ai",
                    "mode": "plan",
                    "local": {"cwd": os.getcwd()},
                },
            )
        except Exception as exc:
            raise GitHubError(f"Cursor SDK review failed: {exc}") from exc
    except Exception as exc:
        raise GitHubError(f"Cursor SDK review failed: {exc}") from exc

    status = getattr(result, "status", None)
    text = (getattr(result, "result", None) or "").strip()
    if status != "finished":
        raise GitHubError(
            f"Cursor review run status={status!r} "
            f"agent_id={getattr(result, 'agent_id', '')} "
            f"result={text[:400]!r}"
        )
    if not text:
        raise GitHubError("Cursor review returned empty content")
    return text, model


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
    """Cursor first (when keyed), then OpenAI, then GitHub Models."""
    errors: list[str] = []
    force = (os.environ.get("REVIEW_PROVIDER") or "").strip().lower()

    def _try_cursor() -> tuple[str, str] | None:
        if not cursor_api_key():
            return None
        try:
            return chat_cursor(
                system, user, model=os.environ.get("CURSOR_MODEL") or model
            )
        except Exception as exc:
            errors.append(f"cursor: {exc}")
            return None

    def _try_openai() -> tuple[str, str] | None:
        if not openai_api_key():
            return None
        try:
            return chat_openai(
                system, user, model=os.environ.get("OPENAI_MODEL") or model
            )
        except Exception as exc:
            errors.append(f"openai: {exc}")
            return None

    if force in {"cursor", "cursor-sdk", "composer"}:
        order = ["cursor", "openai", "github-models"]
    elif force in {"openai", "chatgpt"}:
        order = ["openai", "cursor", "github-models"]
    elif force in {"github-models", "models"}:
        order = ["github-models"]
    else:
        # Prefer Cursor whenever the key exists (OpenAI quota is often exhausted).
        order = ["cursor", "openai", "github-models"]

    for name in order:
        if name == "cursor":
            got = _try_cursor()
            if got:
                return got
        elif name == "openai":
            got = _try_openai()
            if got:
                return got
        elif name == "github-models":
            try:
                return chat_github(system, user, model=model)
            except Exception as exc:
                errors.append(f"github-models: {exc}")

    raise GitHubError("review chat failed: " + " | ".join(errors or ["no providers"]))


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
        "Request changes if the PR is a no-op, scaffold sync, product-unrelated files, "
        "or leaves acceptance criteria unmet.\n"
        "Also request changes when mobile screenshots show hero/primary text clipped "
        "or overflowing the viewport (out of frame) — that is a Builder CSS fix.\n"
        "Also request changes when ADMIN_PREVIEW_MODE admin data pages show empty "
        "shells (“no … yet”, empty tables, placeholder milestone copy) — Builder "
        "must ship randomized mock rows in app/admin_preview.py for screenshots.\n"
        "Screenshot policy (docs/SCREENSHOTS.md) — do NOT request changes for:\n"
        "- missing saberistic.com / production `pre-*.png` on the PR (pre-merge captures "
        "PR-head `branch-*.png` only; production shots are post-deploy)\n"
        "- missing `/admin` screenshots when Reviewer evidence already includes "
        "`branch-admin*.png` under ADMIN_PREVIEW_MODE, OR when the PR is admin-only "
        "and posted a skip/branch note — admin is never shot on saberistic.com\n"
        "- files under `.agent/screenshots/` (allowed Reviewer evidence)\n"
        "- noisy/file-by-file commit history (gate squash-merges to main)\n"
        "- wording/style nits when acceptance criteria are met\n"
        "If acceptance criteria are met, set decision=approved and meets_acceptance=true.\n"
        "Be concrete in reasons.\n"
    )
    user = json.dumps(ctx, indent=2)
    raw, model = chat(system, user, model=model)
    data = extract_json(raw)
    decision = str(data.get("decision") or "").lower().replace("_", "-")
    meets = bool(data.get("meets_acceptance"))
    if decision not in {"approved", "changes-requested"}:
        decision = "approved" if meets else "changes-requested"
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    # Drop nit reasons that must not block when acceptance is met (#58).
    nit_re = re.compile(
        r"screenshot|\.agent/screenshots|commit history|squash|nit\b|wording|style|"
        r"branch-admin|pre-.*\.png|saberistic\.com|production baseline|"
        r"ADMIN_PREVIEW|admin.*(shot|png|screenshot)",
        re.I,
    )
    issue_blob = f"{ctx.get('issue_title')}\n{ctx.get('issue_body')}".lower()
    infra_issue = bool(
        re.search(r"screenshot|headless|playwright|visual (check|evidence)", issue_blob)
    )
    if looks_like_scaffold_sync(ctx) and not infra_issue:
        decision = "changes-requested"
        meets = False
        reasons = [
            "PR looks like Builder scaffold sync (sync commits / boilerplate body), "
            "not an implementation of the issue"
        ] + [str(r) for r in reasons]
    elif looks_like_scaffold_sync(ctx) and infra_issue:
        decision = "approved"
        meets = True
        reasons = [
            "Infra/screenshot issue: docs sync acceptable when screenshot scripts "
            "already live on main"
        ] + [str(r) for r in reasons]
    elif meets and decision == "changes-requested":
        # Acceptance met + only nits → approve (learned from #58 review loop).
        non_nits = [str(r) for r in reasons if not nit_re.search(str(r))]
        if not non_nits:
            decision = "approved"
            reasons = ["Acceptance criteria met; remaining notes are nits only"] + [
                str(r) for r in reasons
            ]
        else:
            reasons = non_nits
    else:
        # Rejecting solely for missing admin / production-pre shots is a policy miss.
        policy_re = re.compile(
            r"branch-admin|admin-login\.png|"
            r"(missing|require).*(admin|branch-admin).*(png|screenshot)|"
            r"admin.*(png|screenshot|shot|visual)|"
            r"(screenshot|visual).*(/admin|admin page)|"
            r"production.*(baseline|pre|screenshot)|"
            r"saberistic\.com.*(shot|screenshot|pre)|"
            r"ADMIN_PREVIEW",
            re.I,
        )
        other = [str(r) for r in reasons if not policy_re.search(str(r))]
        policy_hits = [str(r) for r in reasons if policy_re.search(str(r))]
        if (
            decision == "changes-requested"
            and policy_hits
            and not other
        ):
            decision = "approved"
            reasons = [
                "Screenshot policy: admin uses ADMIN_PREVIEW_MODE on PR branch; "
                "saberistic.com shots are post-deploy only"
            ] + policy_hits
    return {
        "decision": decision,
        "meets_acceptance": True if decision == "approved" else False,
        "reasons": [str(r) for r in reasons][:12],
        "summary": str(data.get("summary") or ""),
        "model": model,
        "scaffold_sync": looks_like_scaffold_sync(ctx),
    }
