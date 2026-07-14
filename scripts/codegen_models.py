#!/usr/bin/env python3
"""Generate a small product change via GitHub Models and open a Builder PR."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from github_api import GitHubError, api, post_issue_comment, put_files, split_repo, token
from pr_labels import apply_pr_mirror

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
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
UI_CONTEXT_FILES = (
    "site/index.html",
    "site/assets/site.css",
    "docs/LANDING.md",
    "docs/DESIGN.md",
    "tests/test_api.py",
    "AGENTS/builder.md",
)


def models_token() -> str:
    value = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not value:
        raise GitHubError("missing MODELS_TOKEN / GITHUB_TOKEN for GitHub Models")
    return value


def openai_api_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY")
    return value.strip() if value and value.strip() else None


def is_agent_infra_issue(title: str, body: str) -> bool:
    """True only for Reviewer/screenshot *infra* work, not product UI issues.

    Product issues often mention screenshots in acceptance criteria; those must
    still go through OpenAI/codegen, not the docs-sync shortcut.
    """
    title_l = title.lower()
    text = f"{title}\n{body}".lower()
    # Landing / LinkedIn / hero CTA product work is never infra.
    if re.search(
        r"\blanding\b|\blinkedin\b|\bhero\b|\bcta\b|\babout page\b",
        text,
    ) and not re.search(
        r"\breviewer:\s*(headless|screenshot)|\bscreenshot.*(workflow|infra|pipeline)\b",
        title_l,
    ):
        return False
    return bool(
        re.search(
            r"\breviewer:\s*(headless|screenshot)|\bheadless\b.*\bscreenshot\b|"
            r"\bplaywright\b|\bscreenshot.*(workflow|infra|pipeline|before approve)\b|"
            r"visual (check|evidence|proof).*before approve|after deploy.*screenshot",
            text,
        )
    )


def is_ui_design_issue(title: str, body: str) -> bool:
    text = f"{title}\n{body}".lower()
    # Product landing/CTA work wins even if AC mentions Reviewer screenshots.
    if re.search(
        r"\blanding\b|\bhero\b|\bcta\b|\blinkedin\b|\babout page\b",
        text,
    ) and not re.search(
        r"\breviewer:\s*headless|\bscreenshot.*workflow\b|\bplaywright\b.*\b(reviewer|approve)\b",
        text,
    ):
        return bool(
            re.search(
                r"\bui\b|\bux\b|\blanding\b|\bdesign\b|\bcss\b|\bhero\b|\bcta\b|"
                r"\blayout\b|\babout page\b|saberistic\.com|\blinkedin\b",
                text,
            )
        )
    # Pure infra / agent-loop tooling is not a product UI redesign.
    if is_agent_infra_issue(title, body):
        return False
    return bool(
        re.search(
            r"\bui\b|\bux\b|\blanding\b|\bdesign\b|\bcss\b|\bhero\b|\bcta\b|"
            r"\blayout\b|\babout page\b|saberistic\.com",
            text,
        )
    )


BINARY_PATH_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".zip",
)


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "work")[:limit]


def is_binary_path(path: str) -> bool:
    """Return True when Contents API must preserve raw bytes (not UTF-8 text)."""
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in BINARY_PATH_SUFFIXES)


def resolve_builder_branch(
    repo: str, issue: int, title: str
) -> tuple[str, dict[str, Any] | None]:
    """Prefer an open linked PR head so Builder never forks a parallel branch.

    Title-derived slugs drift (e.g. ``P1 — …`` vs bare title) and previously
    created ghost branches that Reviewer never sees.
    """
    prs = linked_open_prs(repo, issue)
    if prs:
        pr = prs[0]
        head = ((pr.get("head") or {}).get("ref") or "").strip()
        if head:
            return head, pr
    return f"builder/{issue}-{slugify(title)}", None


def repo_context(cwd: Path, *, ui: bool = False) -> str:
    lines: list[str] = ["## Repository snapshot"]
    entries: list[str] = []
    for path in sorted(cwd.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(cwd).as_posix()
        if rel.startswith((".git/", ".venv/", "trace/", ".agent/")):
            continue
        if path.suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pyc"}:
            continue
        entries.append(rel)
        if len(entries) >= 80:
            break
    lines.append("### Paths\n" + "\n".join(f"- {e}" for e in entries))

    lines.append("\n### Key file contents")
    for rel in UI_CONTEXT_FILES if ui else CONTEXT_FILES:
        path = cwd / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        limit = 12000 if ui else 6000
        if len(text) > limit:
            text = text[:limit] + "\n…[truncated]…"
        lines.append(f"\n#### `{rel}`\n```\n{text}\n```")
    return "\n".join(lines)


def chat_completion_github(*, model: str, system: str, user: str) -> str:
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


def chat_completion_openai(*, model: str, system: str, user: str) -> str:
    key = openai_api_key()
    if not key:
        raise GitHubError("missing OPENAI_API_KEY")
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_URL,
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-builder",
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
        raise GitHubError(f"OpenAI API returned no choices: {body!r}")
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise GitHubError(f"OpenAI API empty content: {body!r}")
    return str(content)


def chat_completion(*, provider: str, model: str, system: str, user: str) -> str:
    if provider == "openai":
        return chat_completion_openai(model=model, system=system, user=user)
    if provider == "cursor":
        raise GitHubError("cursor provider uses build_with_cursor, not chat_completion")
    if provider == "gemini":
        raise GitHubError(
            "Gemini is retired for this repo; set CURSOR_API_KEY / CODEGEN_PROVIDER=cursor"
        )
    return chat_completion_github(model=model, system=system, user=user)


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
        snippet = text[start : end + 1]
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError as exc:
            # Common model failure: raw newlines/tabs inside HTML string values.
            repaired = snippet.replace("\r\n", "\n")
            repaired = re.sub(
                r'(?<!\\)\n',
                r"\\n",
                repaired,
            )
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                raise GitHubError(
                    f"model JSON parse failed ({exc}); first 800 chars: {snippet[:800]!r}"
                ) from exc
    if not isinstance(data, dict):
        raise GitHubError("model JSON root must be an object")
    return data


def validate_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise GitHubError("model plan missing non-empty files[]")
    if len(files) > MAX_FILES:
        raise GitHubError(f"model proposed too many files ({len(files)} > {MAX_FILES})")
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise GitHubError("each files[] entry must be an object")
        path = str(item.get("path") or "").strip().lstrip("/")
        content = item.get("content")
        content_b64 = item.get("content_b64")
        if not path or (content is None and not content_b64):
            raise GitHubError("files[] entries need path and content or content_b64")
        if ".." in path.split("/"):
            raise GitHubError(f"refusing path traversal: {path}")
        if path.startswith((".git/", ".venv/")):
            raise GitHubError(f"refusing protected path: {path}")
        if content_b64:
            try:
                # Models often omit trailing '=' padding; normalize before decode.
                cleaned = re.sub(r"[^A-Za-z0-9+/=]", "", str(content_b64))
                pad = (-len(cleaned)) % 4
                if pad:
                    cleaned = cleaned + ("=" * pad)
                raw = base64.b64decode(cleaned, validate=False)
            except Exception as exc:
                raise GitHubError(f"invalid content_b64 for {path}: {exc}") from exc
            if is_binary_path(path):
                if len(raw) > MAX_FILE_CHARS:
                    raise GitHubError(f"file too large: {path}")
                out.append({"path": path, "content": raw})
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitHubError(
                    f"content_b64 for text path {path} is not valid UTF-8: {exc}"
                ) from exc
        else:
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


def put_file(
    repo: str, branch: str, path: str, content: str | bytes, message: str
) -> None:
    """Write one file via a single Git Data commit (see ``put_files``)."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    put_files(repo, branch, [(path, raw)], message)


def put_file_batch(
    repo: str,
    branch: str,
    items: list[tuple[str, str | bytes]],
    message: str,
) -> None:
    """Write many files in **one** commit (Builder codegen anti-loop)."""
    batch: list[tuple[str, bytes]] = []
    for path, content in items:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        batch.append((path, raw))
    put_files(repo, branch, batch, message)


def linked_open_prs(repo: str, issue: int) -> list[dict]:
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    needle = f"#{issue}"
    return [
        pr
        for pr in prs
        if needle in (pr.get("title") or "") or needle in (pr.get("body") or "")
    ]


def json_system_prompt(*, ui: bool) -> str:
    base = (
        "Return ONLY valid JSON (no markdown outside JSON) with this schema:\n"
        "{\n"
        '  "commit_message": "string",\n'
        '  "pr_summary": "string",\n'
        '  "files": [{"path": "relative/path", "content": "full file text"}]\n'
        "}\n"
        "Rules:\n"
        f"- At most {MAX_FILES} files; prefer minimal diffs.\n"
        "- Prefer plain UTF-8 \"content\" strings (correctly JSON-escaped). "
        "Use content_b64 only if you cannot escape HTML safely.\n"
        "- Never invent typos in imports (FastAPI not FastAPH), HTML tags "
        "(</head> not </`ead>), or CSS selectors (.hero not .herm).\n"
        "- commit_message MUST include the issue id like builder(#123): …\n"
        "- Include full file contents for each touched file.\n"
        "- Add/update tests when behavior changes.\n"
        "- New admin/UI pages MUST include ADMIN_PREVIEW_MODE randomized mock "
        "data via app/admin_preview.py builders (and tests) so Reviewer "
        "screenshots are not empty shells.\n"
        "- Do not invent secrets, credentials, or unrelated refactors.\n"
        "- Do not modify .github/workflows agent orchestration unless required.\n"
        "- Stay within the issue scope.\n"
    )
    if not ui:
        return (
            "You are a careful software engineer implementing ONE GitHub issue.\n"
            + base
        )
    return (
        "You are a senior product designer + front-end engineer.\n"
        "Optimize for a brutal-minimalist, brand-first landing page.\n"
        "Brand colors lean deep navy (#0c0f18 / #171d34) with orange accent (#d88730).\n"
        "Typography: Archivo Black + IBM Plex Mono already in use — keep them unless "
        "the issue asks otherwise.\n"
        "Avoid: purple gradients, cream+serif terracotta, newspaper layouts, "
        "card grids in the hero, pill clusters, glow spam.\n"
        "Prefer one clear composition, one primary CTA, generous type hierarchy, "
        "subtle intentional motion only.\n"
        "Use existing saberistic logos in site/assets/ (do not duplicate mark+wordmark).\n"
        "Do not revive any team roster.\n"
        + base
    )


def cursor_api_key() -> str | None:
    value = os.environ.get("CURSOR_API_KEY")
    return value.strip() if value and value.strip() else None


def select_provider(title: str, body: str) -> tuple[str, str]:
    """Return (provider, model).

    Policy:
    - CODEGEN_PROVIDER force: cursor|openai|chatgpt|github-models|models
    - Else Cursor when CURSOR_API_KEY is set (preferred for coding)
    - Else OpenAI when OPENAI_API_KEY is set
    - Else GitHub Models last-resort backup
    """
    del title, body
    force = (os.environ.get("CODEGEN_PROVIDER") or "").strip().lower()
    if force in {"cursor", "cursor-sdk", "composer"}:
        return "cursor", os.environ.get("CURSOR_MODEL") or "composer-2.5"
    if force in {"openai", "chatgpt"}:
        return "openai", os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    if force in {"github-models", "models"}:
        return "github-models", os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    if force == "gemini":
        raise GitHubError(
            "CODEGEN_PROVIDER=gemini is retired; use cursor or openai"
        )

    if cursor_api_key():
        return "cursor", os.environ.get("CURSOR_MODEL") or "composer-2.5"
    if openai_api_key():
        return "openai", os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    return "github-models", os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL


def _other_provider(provider: str) -> tuple[str, str]:
    """Backup chain: cursor → openai → github-models (and reverse)."""
    openai_model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    models_model = os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    cursor_model = os.environ.get("CURSOR_MODEL") or "composer-2.5"
    if provider == "cursor":
        if openai_api_key():
            return "openai", openai_model
        return "github-models", models_model
    if provider == "openai":
        if cursor_api_key():
            return "cursor", cursor_model
        return "github-models", models_model
    if cursor_api_key():
        return "cursor", cursor_model
    return "openai", openai_model


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
    ui = is_ui_design_issue(title, body)
    provider, model = select_provider(title, body)
    if provider == "cursor":
        from codegen_cursor import build_with_cursor

        return build_with_cursor(
            repo, issue, title=title, body=body, brief=brief
        )
    force = (os.environ.get("CODEGEN_PROVIDER") or "").strip().lower()
    if (
        provider == "openai"
        and not openai_api_key()
        and force not in {"github-models", "models"}
    ):
        raise GitHubError(
            "OPENAI_API_KEY missing. Prefer CURSOR_API_KEY / CODEGEN_PROVIDER=cursor, "
            "or set OPENAI_API_KEY, or force CODEGEN_PROVIDER=github-models."
        )
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]
    branch, existing_pr = resolve_builder_branch(repo, issue, title)

    system = json_system_prompt(ui=ui)
    # Thin planner child issues often only say "User flow" — pull parent context.
    parent = ""
    m = re.search(r"(?:Parent|Child of)\s*:?\s*#(\d+)", body, re.I)
    if m:
        try:
            pdata = api("GET", f"/repos/{owner}/{name}/issues/{m.group(1)}")
            parent = (
                f"\n## Parent issue #{m.group(1)}: {pdata.get('title')}\n"
                f"{(pdata.get('body') or '')[:12000]}\n"
            )
        except Exception:
            parent = ""
    user = (
        f"Repository: {repo}\n"
        f"Issue: #{issue}\n"
        f"Title: {title}\n"
        f"UI/design focus: {ui}\n"
        f"Live reference (if relevant): https://saberistic.com/\n\n"
        f"## Issue body\n{body.strip() or '(empty)'}\n"
        f"{parent}\n"
        f"## Builder brief\n{brief_text[:5000]}\n\n"
        f"{repo_context(root, ui=ui)}\n"
    )

    try:
        raw = chat_completion(provider=provider, model=model, system=system, user=user)
    except Exception as primary_exc:
        alt_provider, alt_model = _other_provider(provider)
        if alt_provider == "cursor" and cursor_api_key():
            from codegen_cursor import build_with_cursor

            result = build_with_cursor(
                repo, issue, title=title, body=body, brief=brief
            )
            result["note"] = f"{provider}_failed: {primary_exc}; used_cursor"
            return result
        if alt_provider == "openai" and not openai_api_key():
            raise GitHubError(
                f"{provider} failed: {primary_exc}\n"
                "OpenAI backup unavailable (missing OPENAI_API_KEY). "
                "Set CURSOR_API_KEY for Cursor SDK codegen."
            ) from primary_exc
        try:
            raw = chat_completion(
                provider=alt_provider, model=alt_model, system=system, user=user
            )
            primary_name = provider
            provider, model = alt_provider, alt_model
            fallback_note = f"{primary_name}_failed: {primary_exc}; used_{alt_provider}"
        except Exception as backup_exc:
            raise GitHubError(
                f"Primary ({provider}) failed: {primary_exc}\n"
                f"Backup ({alt_provider}) failed: {backup_exc}"
            ) from backup_exc
    else:
        fallback_note = None

    try:
        plan = extract_json(raw)
        files = validate_plan(plan)
    except Exception as parse_exc:
        repair_user = (
            f"{user}\n\n"
            "## Previous invalid model output (fix and return valid JSON only)\n"
            f"Error: {parse_exc}\n"
            "Return the same change using plain UTF-8 \"content\" for EVERY file "
            "(correct JSON string escaping). Avoid content_b64. No markdown fences.\n"
        )
        raw = chat_completion(
            provider=provider, model=model, system=system, user=repair_user
        )
        plan = extract_json(raw)
        files = validate_plan(plan)
        note = f"json_repaired_after: {parse_exc}"
        fallback_note = f"{fallback_note}; {note}" if fallback_note else note

    commit_message = str(plan.get("commit_message") or f"builder(#{issue}): implement change")
    if f"#{issue}" not in commit_message:
        commit_message = f"builder(#{issue}): {commit_message}"
    pr_summary = str(plan.get("pr_summary") or "Automated Builder change.")

    ensure_branch(repo, branch, base_sha)
    put_file_batch(
        repo,
        branch,
        [(item["path"], item["content"]) for item in files],
        commit_message,
    )

    if existing_pr:
        pr_number = int(existing_pr["number"])
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
                    f"### Codegen\n"
                    f"- provider: `{provider}`\n"
                    f"- model: `{model}`\n"
                    f"- ui_design: `{str(ui).lower()}`\n"
                    f"- files: {', '.join(f'`{f['path']}`' for f in files)}\n"
                ),
            },
        )
        pr_number = int(pr["number"])
        created = True

    apply_pr_mirror(
        repo,
        issue,
        pr_number,
        default_review="review:needs-review",
    )

    comment = (
        "### builder_result\n"
        f"- kind: `{provider}`\n"
        f"- model: `{model}`\n"
        f"- ui_design: `{str(ui).lower()}`\n"
        f"- branch: `{branch}`\n"
        f"- pr: #{pr_number}\n"
        f"- files: {', '.join(f'`{f['path']}`' for f in files)}\n"
        f"- created_pr: `{str(created).lower()}`\n"
    )
    if fallback_note:
        comment += f"- note: `{fallback_note}`\n"
    post_issue_comment(repo, issue, comment)
    return {
        "provider": provider,
        "model": model,
        "ui_design": ui,
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
