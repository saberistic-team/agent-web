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

from github_api import GitHubError, api, post_issue_comment, split_repo, token

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
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


def gemini_api_key() -> str | None:
    value = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return value.strip() if value and value.strip() else None


def is_agent_infra_issue(title: str, body: str) -> bool:
    """True only for Reviewer/screenshot *infra* work, not product UI issues.

    Product issues often mention screenshots in acceptance criteria; those must
    still go through Gemini/codegen, not the docs-sync shortcut.
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


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "work")[:limit]


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


def chat_completion_gemini(*, model: str, system: str, user: str) -> str:
    key = gemini_api_key()
    if not key:
        raise GitHubError("missing GEMINI_API_KEY")
    url = GEMINI_URL_TMPL.format(model=urllib.parse.quote(model, safe=""))
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}?key={urllib.parse.quote(key)}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "agent-web-builder",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"Gemini API ({model}) -> {exc.code}: {detail}") from exc

    candidates = body.get("candidates") or []
    if not candidates:
        raise GitHubError(f"Gemini ({model}) returned no candidates: {body!r}")
    finish = candidates[0].get("finishReason")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
    content = "\n".join(t for t in texts if t).strip()
    if not content:
        raise GitHubError(
            f"Gemini ({model}) empty content (finishReason={finish}): {body!r}"
        )
    return content


def chat_completion(*, provider: str, model: str, system: str, user: str) -> str:
    if provider == "gemini":
        # Try configured model, then known-good aliases if the id is retired.
        models = [model]
        for alt in ("gemini-3.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"):
            if alt not in models:
                models.append(alt)
        errors: list[str] = []
        for mid in models:
            try:
                return chat_completion_gemini(model=mid, system=system, user=user)
            except Exception as exc:
                errors.append(str(exc))
                # Only rotate on missing-model / not-found style failures.
                if not re.search(r"\b404\b|NOT_FOUND|no longer available|not found", str(exc), re.I):
                    raise
        raise GitHubError("Gemini failed for all models: " + " | ".join(errors))
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
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:
                raise GitHubError(f"invalid content_b64 for {path}: {exc}") from exc
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


def json_system_prompt(*, ui: bool) -> str:
    base = (
        "Return ONLY valid JSON (no markdown outside JSON) with this schema:\n"
        "{\n"
        '  "commit_message": "string",\n'
        '  "pr_summary": "string",\n'
        '  "files": [{"path": "relative/path", "content_b64": "base64 utf-8 file"}]\n'
        "}\n"
        "Rules:\n"
        f"- At most {MAX_FILES} files; prefer minimal diffs.\n"
        "- Prefer content_b64 (standard base64 of the full UTF-8 file) so HTML/CSS "
        "never breaks JSON escaping. Plain \"content\" string is allowed only for "
        "tiny non-HTML files.\n"
        "- commit_message MUST include the issue id like builder(#123): …\n"
        "- Include full file contents for each touched file.\n"
        "- Add/update tests when behavior changes.\n"
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


def select_provider(title: str, body: str) -> tuple[str, str]:
    """Return (provider, model).

    Policy:
    - Visual/UI/design issues → Gemini primary (when key set), else GitHub Models
    - Everything else → GitHub Models primary (free Actions Models), else Gemini
    - Either side falls back to the other in build_with_models on failure
    """
    force = (os.environ.get("CODEGEN_PROVIDER") or "").strip().lower()
    if force in {"gemini", "github-models", "models"}:
        if force == "gemini":
            return "gemini", os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        return "github-models", os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL

    ui = is_ui_design_issue(title, body)
    has_gemini = bool(gemini_api_key())
    models_model = os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    gemini_model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL

    if ui and has_gemini:
        return "gemini", gemini_model
    if not ui:
        return "github-models", models_model
    # UI but no Gemini key → Models
    return "github-models", models_model


def _other_provider(provider: str) -> tuple[str, str]:
    if provider == "gemini":
        return "github-models", os.environ.get("GITHUB_MODELS_MODEL") or DEFAULT_MODEL
    return "gemini", os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL


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
    brief_text = brief.read_text(encoding="utf-8") if brief.is_file() else ""
    owner, name = split_repo(repo)
    default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
    ref = api("GET", f"/repos/{owner}/{name}/git/ref/heads/{default}")
    base_sha = ref["object"]["sha"]
    branch = f"builder/{issue}-{slugify(title)}"

    system = json_system_prompt(ui=ui)
    user = (
        f"Repository: {repo}\n"
        f"Issue: #{issue}\n"
        f"Title: {title}\n"
        f"UI/design focus: {ui}\n"
        f"Live reference (if relevant): https://saberistic.com/\n\n"
        f"## Issue body\n{body.strip() or '(empty)'}\n\n"
        f"## Builder brief\n{brief_text[:5000]}\n\n"
        f"{repo_context(root, ui=ui)}\n"
    )

    try:
        raw = chat_completion(provider=provider, model=model, system=system, user=user)
    except Exception as primary_exc:
        # Mutual backup: Models ↔ Gemini when primary is unavailable / not permissioned.
        alt_provider, alt_model = _other_provider(provider)
        if alt_provider == "gemini" and not gemini_api_key():
            raise GitHubError(
                f"{provider} failed: {primary_exc}\n"
                "Gemini backup unavailable (missing GEMINI_API_KEY)."
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
        # One repair pass: force content_b64 so HTML cannot break JSON.
        repair_user = (
            f"{user}\n\n"
            "## Previous invalid model output (fix and return valid JSON only)\n"
            f"Error: {parse_exc}\n"
            "Return the same change using content_b64 for EVERY file "
            "(standard base64 of full UTF-8 contents). No markdown fences.\n"
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
