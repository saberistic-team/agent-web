#!/usr/bin/env python3
"""Generate a small product change via GitHub Models and open a Builder PR."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from github_api import GitHubError, api, post_issue_comment, split_repo, token

DEFAULT_MODEL = "openai/gpt-4o-mini"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
MAX_FILES = 12
MAX_FILE_CHARS = 80_000
CONTEXT_FILES = (
    "README.md",
    "requirements.txt",
    "app/main.py",
    "tests/test_api.py",
    "docs/LABELS.md",
    "AGENTS/builder.md",
)


def models_token() -> str:
    value = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not value:
        raise GitHubError("missing MODELS_TOKEN / GITHUB_TOKEN for GitHub Models")
    return value


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "work")[:limit]


def repo_context(cwd: Path) -> str:
    lines: list[str] = ["## Repository snapshot"]
    # Shallow file list
    entries: list[str] = []
    for path in sorted(cwd.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(cwd).as_posix()
        if rel.startswith((".git/", ".venv/", "trace/", ".agent/")):
            continue
        if any(part.startswith(".") and part not in {".github"} for part in path.parts):
            if ".github" not in path.parts:
                continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pyc"}:
            continue
        entries.append(rel)
        if len(entries) >= 80:
            break
    lines.append("### Paths\n" + "\n".join(f"- {e}" for e in entries))

    lines.append("\n### Key file contents")
    for rel in CONTEXT_FILES:
        path = cwd / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > 6000:
            text = text[:6000] + "\n…[truncated]…"
        lines.append(f"\n#### `{rel}`\n```\n{text}\n```")
    return "\n".join(lines)


def chat_completion(*, model: str, system: str, user: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MODELS_URL,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {models_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-builder",
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
        raise GitHubError(f"Models API returned no choices: {body!r}")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise GitHubError(f"Models API empty content: {body!r}")
    return str(content)


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise GitHubError(f"model did not return JSON: {text[:500]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise GitHubError("model JSON root must be an object")
    return data


def validate_plan(plan: dict[str, Any]) -> list[dict[str, str]]:
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise GitHubError("model plan missing non-empty files[]")
    if len(files) > MAX_FILES:
        raise GitHubError(f"model proposed too many files ({len(files)} > {MAX_FILES})")
    out: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise GitHubError("each files[] entry must be an object")
        path = str(item.get("path") or "").strip().lstrip("/")
        content = item.get("content")
        if not path or content is None:
            raise GitHubError("files[] entries need path and content")
        if ".." in path.split("/"):
            raise GitHubError(f"refusing path traversal: {path}")
        if path.startswith((".git/", ".venv/")):
            raise GitHubError(f"refusing protected path: {path}")
        text = content if isinstance(content, str) else json.dumps(content, indent=2)
        if len(text) > MAX_FILE_CHARS:
            raise GitHubError(f"file too large: {path}")
        out.append({"path": path, "content": text})
    return out


def ensure_branch(repo: str, branch: str, base_sha: str) -> None:
    owner, name = split_repo(repo)
    try:
        api(
            "POST",
            f"/repos/{owner}/{name}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
    except GitHubError as exc:
        if "Reference already exists" not in str(exc):
            raise


def put_file(repo: str, branch: str, path: str, content: str, message: str) -> None:
    owner, name = split_repo(repo)
    # Ensure Contents API always has a GitHub token for mutations.
    token()
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    put_body: dict[str, Any] = {
        "message": message,
        "content": content_b64,
        "branch": branch,
    }
    try:
        existing = api("GET", f"/repos/{owner}/{name}/contents/{path}?ref={branch}")
        put_body["sha"] = existing["sha"]
    except GitHubError:
        pass
    api("PUT", f"/repos/{owner}/{name}/contents/{path}", body=put_body)


def linked_open_prs(repo: str, issue: int) -> list[dict]:
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    needle = f"#{issue}"
    return [
        pr
        for pr in prs
        if needle in (pr.get("title") or "") or needle in (pr.get("body") or "")
    ]


def build_with_models(
    repo: str,
    issue: int,
    *,
    title: str,
    body: str,
    brief: Path,
    cwd: Path | None = None,
) -> dict[str, Any]:
    root = cwd or Path.cwd()
    model = os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]
    branch = f"builder/{issue}-{slugify(title)}"

    system = (
        "You are a careful software engineer implementing ONE GitHub issue.\n"
        "Return ONLY valid JSON (no markdown outside JSON) with this schema:\n"
        "{\n"
        '  "commit_message": "string",\n'
        '  "pr_summary": "string",\n'
        '  "files": [{"path": "relative/path", "content": "full file contents"}]\n'
        "}\n"
        "Rules:\n"
        f"- At most {MAX_FILES} files; prefer minimal diffs.\n"
        "- Include full file contents for each touched file.\n"
        "- Add/update tests when behavior changes.\n"
        "- Do not invent secrets, credentials, or unrelated refactors.\n"
        "- Do not modify .github/workflows agent orchestration unless required.\n"
        "- Stay within the issue scope.\n"
    )
    user = (
        f"Repository: {repo}\n"
        f"Issue: #{issue}\n"
        f"Title: {title}\n\n"
        f"## Issue body\n{body.strip() or '(empty)'}\n\n"
        f"## Builder brief\n{brief_text[:5000]}\n\n"
        f"{repo_context(root)}\n"
    )

    raw = chat_completion(model=model, system=system, user=user)
    plan = extract_json(raw)
    files = validate_plan(plan)
    commit_message = str(plan.get("commit_message") or f"builder(#{issue}): implement change")
    pr_summary = str(plan.get("pr_summary") or "Automated Builder change via GitHub Models.")

    ensure_branch(repo, branch, base_sha)
    for item in files:
        put_file(
            repo,
            branch,
            item["path"],
            item["content"],
            f"{commit_message} ({item['path']})",
        )

    prs = linked_open_prs(repo, issue)
    if prs:
        pr_number = int(prs[0]["number"])
        created = False
    else:
        pr = api(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            body={
                "title": f"builder: {title} (#{issue})",
                "head": branch,
                "base": default,
                "body": (
                    f"Closes #{issue}\n\n"
                    f"{pr_summary}\n\n"
                    f"### Models\n"
                    f"- provider: `github-models`\n"
                    f"- model: `{model}`\n"
                    f"- files: {', '.join(f'`{f['path']}`' for f in files)}\n"
                ),
            },
        )
        pr_number = int(pr["number"])
        created = True

    post_issue_comment(
        repo,
        issue,
        (
            "### builder_result\n"
            "- kind: `github-models`\n"
            f"- model: `{model}`\n"
            f"- branch: `{branch}`\n"
            f"- pr: #{pr_number}\n"
            f"- files: {', '.join(f'`{f['path']}`' for f in files)}\n"
            f"- created_pr: `{str(created).lower()}`\n"
        ),
    )
    return {
        "model": model,
        "branch": branch,
        "pr": pr_number,
        "files": [f["path"] for f in files],
        "created_pr": created,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--brief", type=Path, default=Path("AGENTS/builder.md"))
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    args = parser.parse_args(argv)
    try:
        owner, name = split_repo(args.repo)
        data = api("GET", f"/repos/{owner}/{name}/issues/{args.issue}")
        title = args.title or data.get("title") or f"issue-{args.issue}"
        body = args.body or data.get("body") or ""
        result = build_with_models(
            args.repo,
            args.issue,
            title=title,
            body=body,
            brief=args.brief,
        )
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
