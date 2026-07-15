#!/usr/bin/env python3
"""Builder helpers: detect and resolve PR merge conflicts using recent work.

When an open Builder PR falls behind ``main`` (often after other issues merge),
Builder reviews recently **merged PRs** and **closed issues**, then rebases /
merges the PR head and resolves text conflicts with that context.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from github_api import GitHubError, api, post_issue_comment, split_repo

CONFLICT_MARKER = re.compile(r"^<<<<<<< ", re.M)
DEFAULT_RECENT_PRS = 8
DEFAULT_RECENT_ISSUES = 8
MAX_BODY_CHARS = 1200
MAX_FILE_CHARS = 60_000
# Fast import check after conflict resolution. Broken merges that still claim
# ``resolved`` were looping Builder↔Reviewer (e.g. dropped ``admin_router``,
# missing Protocol exports). Fail closed until smoke passes.
SMOKE_IMPORT = "from app.main import app"
SMOKE_TIMEOUT_SEC = 90

# Symbols commonly dropped during conflict/codegen that break ``app.main``.
MAIN_WIRING_IMPORTS: tuple[tuple[str, str], ...] = (
    (
        "admin_router",
        "from app.admin_routes import router as admin_router",
    ),
    (
        "CORRELATION_HEADER",
        "from app.actor_context import CORRELATION_HEADER",
    ),
    (
        "AdminLoginRequired",
        "from app.admin_auth import AdminLoginRequired, login_redirect_url",
    ),
    (
        "login_redirect_url",
        "from app.admin_auth import AdminLoginRequired, login_redirect_url",
    ),
)
_NAMEERROR_RE = re.compile(
    r"NameError: name '([A-Za-z_][A-Za-z0-9_]*)' is not defined"
)


def linked_open_prs(repo: str, issue: int) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    prs = api("GET", f"/repos/{owner}/{name}/pulls?state=open&per_page=100") or []
    needle = f"#{issue}"
    return [
        pr
        for pr in prs
        if needle in (pr.get("title") or "") or needle in (pr.get("body") or "")
    ]


def repair_main_wiring(text: str) -> tuple[str, list[str]]:
    """Ensure known ``app.main`` symbols have their imports. Returns (text, added)."""
    added: list[str] = []
    lines = text.splitlines(keepends=True)
    existing = set(lines)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            insert_at = i + 1
        elif stripped and not stripped.startswith("#") and insert_at:
            break
    for symbol, stmt in MAIN_WIRING_IMPORTS:
        if symbol not in text:
            continue
        # Already imported under this name or as ``router as admin_router``.
        if re.search(rf"\bas {re.escape(symbol)}\b", text) or re.search(
            rf"^from .+ import .*\b{re.escape(symbol)}\b", text, re.M
        ):
            continue
        if any(stmt in line for line in lines):
            continue
        line = stmt + "\n"
        if line in existing:
            continue
        lines.insert(insert_at, line)
        existing.add(line)
        insert_at += 1
        added.append(symbol)
    return "".join(lines), added


def try_repair_main_after_smoke(
    root: Path, smoke_detail: str
) -> tuple[bool, str]:
    """If smoke failed on a known NameError, repair ``app/main.py`` in place."""
    match = _NAMEERROR_RE.search(smoke_detail or "")
    if not match:
        return False, "no NameError to repair"
    main_path = root / "app" / "main.py"
    if not main_path.is_file():
        return False, "app/main.py missing"
    original = main_path.read_text(encoding="utf-8")
    fixed, added = repair_main_wiring(original)
    if not added or fixed == original:
        return False, f"no wiring repair for {match.group(1)}"
    main_path.write_text(fixed, encoding="utf-8")
    return True, f"added imports for: {', '.join(added)}"


def smoke_import_app(cwd: Path) -> tuple[bool, str]:
    """Import ``app.main`` after a merge; return ``(ok, detail)``."""
    env = {**os.environ, "PYTHONPATH": str(cwd)}
    # Preview/auth settings are optional for import; avoid requiring secrets.
    env.setdefault("ADMIN_PREVIEW_MODE", "1")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", SMOKE_IMPORT],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=SMOKE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"smoke timed out after {SMOKE_TIMEOUT_SEC}s ({SMOKE_IMPORT})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return False, detail[:800]
    return True, "ok"


def smoke_and_maybe_repair_app(cwd: Path) -> tuple[bool, str, list[str]]:
    """Smoke ``app.main``; attempt one known-import repair on NameError."""
    ok, detail = smoke_import_app(cwd)
    repairs: list[str] = []
    if ok:
        return True, detail, repairs
    repaired, note = try_repair_main_after_smoke(cwd, detail)
    if not repaired:
        return False, detail, repairs
    repairs.append(note)
    ok2, detail2 = smoke_import_app(cwd)
    if ok2:
        return True, detail2, repairs
    return False, detail2, repairs


def clone_pr_head(repo: str, pr: dict[str, Any], *, dest: Path) -> Path:
    """Shallow-clone the PR head ref into ``dest`` (must not exist)."""
    owner, name = split_repo(repo)
    head_ref = (pr.get("head") or {}).get("ref")
    if not head_ref:
        raise GitHubError(f"PR #{pr.get('number')} missing head.ref")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise GitHubError("missing GITHUB_TOKEN for PR head smoke clone")
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["clone", "--branch", head_ref, "--single-branch", clone_url, str(dest)],
        cwd=parent,
    )
    return dest


def smoke_pr_head(
    repo: str,
    pr: dict[str, Any],
    *,
    push_repair: bool = True,
) -> dict[str, Any]:
    """Clone PR head and smoke-import ``app.main`` (even when mergeable/clean).

    Optional wiring repair is committed + pushed when smoke recovers, so stale
    broken heads stop looping Builder↔Reviewer without needing a new conflict.
    """
    pr_number = int(pr["number"])
    head_ref = (pr.get("head") or {}).get("ref") or ""
    with tempfile.TemporaryDirectory(prefix="builder-smoke-") as tmp:
        root = Path(tmp) / "repo"
        clone_pr_head(repo, pr, dest=root)
        ok, detail, repairs = smoke_and_maybe_repair_app(root)
        if ok and repairs and push_repair:
            _run_git(["config", "user.name", "saberistic-agent-web-builder"], cwd=root)
            _run_git(
                ["config", "user.email", "builder@users.noreply.github.com"],
                cwd=root,
            )
            _run_git(["add", "--", "app/main.py"], cwd=root)
            commit = _run_git(
                [
                    "commit",
                    "-m",
                    f"builder: repair app.main wiring after smoke (#{pr_number})",
                ],
                cwd=root,
                check=False,
            )
            if commit.returncode != 0:
                return {
                    "status": "smoke_ok",
                    "pr": pr_number,
                    "head": head_ref,
                    "smoke_error": None,
                    "repairs": repairs,
                    "pushed": False,
                    "note": "repair applied locally but commit failed",
                }
            _run_git(["push", "origin", f"HEAD:{head_ref}"], cwd=root)
            return {
                "status": "smoke_repaired",
                "pr": pr_number,
                "head": head_ref,
                "smoke_error": None,
                "repairs": repairs,
                "pushed": True,
            }
        if ok:
            return {
                "status": "smoke_ok",
                "pr": pr_number,
                "head": head_ref,
                "smoke_error": None,
                "repairs": repairs,
                "pushed": False,
            }
        return {
            "status": "smoke_failed",
            "pr": pr_number,
            "head": head_ref,
            "smoke_error": detail,
            "repairs": repairs,
            "pushed": False,
        }


def refresh_pr(repo: str, pr_number: int) -> dict[str, Any]:
    """Return a fresh PR payload (mergeable fields are computed asynchronously)."""
    owner, name = split_repo(repo)
    return api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")


def pr_needs_conflict_resolution(pr: dict[str, Any]) -> bool:
    """True when GitHub reports the PR cannot merge cleanly into its base."""
    if pr.get("merged") or pr.get("state") == "closed":
        return False
    mergeable = pr.get("mergeable")
    state = (pr.get("mergeable_state") or "").lower()
    if mergeable is False:
        return True
    if state in {"dirty", "draft"}:
        return True
    return False


def linked_pr_conflict_status(repo: str, issue: int) -> dict[str, Any]:
    """Fresh mergeability for the open PR linked to ``issue``.

    Returns ``status`` of ``no_pr``, ``clean``, or ``dirty``, plus PR fields
    when a PR exists. Refreshes once when ``mergeable`` is still null.
    """
    prs = linked_open_prs(repo, issue)
    if not prs:
        return {"status": "no_pr", "issue": issue}
    pr_number = int(prs[0]["number"])
    pr = refresh_pr(repo, pr_number)
    if pr.get("mergeable") is None:
        pr = refresh_pr(repo, pr_number)
    dirty = pr_needs_conflict_resolution(pr)
    return {
        "status": "dirty" if dirty else "clean",
        "issue": issue,
        "pr": pr_number,
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "head": (pr.get("head") or {}).get("ref"),
        "pr_payload": pr,
    }


def format_merge_conflict_hard_fail(status: dict[str, Any]) -> str:
    """Reviewer hard-fail line for a conflicted linked PR."""
    return (
        "PR has merge conflicts with base "
        f"(mergeable=`{status.get('mergeable')}`, "
        f"mergeable_state=`{status.get('mergeable_state')}`) — "
        "return to Builder to resolve on the same PR head"
    )


def list_recent_merged_prs(repo: str, *, limit: int = DEFAULT_RECENT_PRS) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    prs = (
        api(
            "GET",
            f"/repos/{owner}/{name}/pulls?state=closed&sort=updated&direction=desc&per_page={limit}",
        )
        or []
    )
    return [pr for pr in prs if pr.get("merged_at")][:limit]


def list_recent_closed_issues(
    repo: str, *, limit: int = DEFAULT_RECENT_ISSUES
) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    issues = (
        api(
            "GET",
            f"/repos/{owner}/{name}/issues?state=closed&sort=updated&direction=desc"
            f"&per_page={max(limit * 3, 30)}",
        )
        or []
    )
    # Issues API includes PRs; keep true issues only.
    out: list[dict[str, Any]] = []
    for item in issues:
        if item.get("pull_request"):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def pr_changed_paths(repo: str, pr_number: int) -> list[str]:
    owner, name = split_repo(repo)
    files = api("GET", f"/repos/{owner}/{name}/pulls/{pr_number}/files?per_page=100") or []
    paths: list[str] = []
    for item in files:
        path = (item.get("filename") or "").strip()
        if path:
            paths.append(path)
    return paths


def _clip(text: str, limit: int = MAX_BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summarize_recent_closed_work(
    repo: str,
    *,
    pr_limit: int = DEFAULT_RECENT_PRS,
    issue_limit: int = DEFAULT_RECENT_ISSUES,
) -> str:
    """Markdown brief of recently merged PRs and closed issues for conflict resolution."""
    lines = [
        "## Recently merged PRs (preserve their intent when resolving conflicts)",
    ]
    merged = list_recent_merged_prs(repo, limit=pr_limit)
    if not merged:
        lines.append("- (none)")
    for pr in merged:
        number = pr.get("number")
        title = pr.get("title") or ""
        paths = pr_changed_paths(repo, int(number)) if number else []
        path_note = ", ".join(f"`{p}`" for p in paths[:12])
        if len(paths) > 12:
            path_note += ", …"
        lines.append(f"- #{number}: {title}")
        if path_note:
            lines.append(f"  - files: {path_note}")
        body = _clip(pr.get("body") or "")
        if body:
            lines.append(f"  - summary: {body.splitlines()[0][:200]}")

    lines.append("")
    lines.append("## Recently closed issues (do not regress acceptance)")
    closed = list_recent_closed_issues(repo, limit=issue_limit)
    if not closed:
        lines.append("- (none)")
    for issue in closed:
        number = issue.get("number")
        title = issue.get("title") or ""
        lines.append(f"- #{number}: {title}")
        body = _clip(issue.get("body") or "", 400)
        if body:
            first = next((ln for ln in body.splitlines() if ln.strip()), "")
            if first:
                lines.append(f"  - note: {first[:200]}")
    return "\n".join(lines)


def format_conflict_resolution_brief(
    repo: str,
    issue: int,
    pr: dict[str, Any],
    *,
    conflicted_paths: list[str] | None = None,
) -> str:
    """Prompt / comment body for resolving conflicts on a Builder PR."""
    head = ((pr.get("head") or {}).get("ref")) or "?"
    base = ((pr.get("base") or {}).get("ref")) or "main"
    number = pr.get("number")
    paths = conflicted_paths or []
    recent = summarize_recent_closed_work(repo)
    path_lines = "\n".join(f"- `{p}`" for p in paths) or "- (unknown until merge runs)"
    return (
        f"Resolve merge conflicts for issue #{issue} on PR #{number}.\n"
        f"Head branch `{head}` must merge cleanly into `{base}`.\n\n"
        "Rules:\n"
        "- Prefer keeping both intents when possible (feature + recently merged work).\n"
        "- Never invent a second Builder branch; stay on this PR head.\n"
        "- Do not drop technical SEO / tests / routes from recently merged PRs.\n"
        "- Binary files: keep valid bytes; do not UTF-8-corrupt PNG/JPEG.\n"
        "- Output full resolved file contents for each conflicted path.\n\n"
        f"### Conflicted paths\n{path_lines}\n\n"
        f"{recent}\n"
    )


def _run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitHubError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    return proc


def list_unmerged_paths(cwd: Path) -> list[str]:
    proc = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd, check=False)
    paths = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return paths


def strip_conflict_markers_prefer_head(text: str) -> str:
    """Naive fallback: keep 'ours' (PR head) side of conflict markers."""
    if not CONFLICT_MARKER.search(text):
        return text
    out: list[str] = []
    mode = "keep"  # keep | skip
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            mode = "keep"
            continue
        if line.startswith("======="):
            mode = "skip"
            continue
        if line.startswith(">>>>>>> "):
            mode = "keep"
            continue
        if mode == "keep":
            out.append(line)
    return "".join(out)


ResolveFn = Callable[[str, str, str], str]


def leftover_conflict_markers(cwd: Path, paths: list[str]) -> list[str]:
    """Return relative paths that still contain git conflict markers."""
    dirty: list[str] = []
    for rel in paths:
        path = cwd / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CONFLICT_MARKER.search(text):
            dirty.append(rel)
    return dirty


def default_resolve_file(
    path: str,
    conflicted_text: str,
    brief: str,
    *,
    chat: ResolveFn | None = None,
) -> str:
    """Resolve one conflicted text file; optional chat(system, user) -> content."""
    if chat is None or path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf")
    ):
        return strip_conflict_markers_prefer_head(conflicted_text)
    system = (
        "You resolve git merge conflicts for a product repo. "
        "Return ONLY the full resolved file contents — no markdown fences, "
        "no explanation. Preserve both feature intent and recently merged work. "
        "Never drop imports, router wiring (`admin_router`), Protocol/repository "
        "exports, cookie/session symbol names, or migration catalog entries that "
        "either side defines — union both sides when unsure. Prefer keeping "
        "main's auth/session APIs when they conflict with obsolete Basic-auth tests."
    )
    user = (
        f"{brief}\n\n"
        f"### Conflicted file `{path}`\n"
        f"```\n{conflicted_text[:MAX_FILE_CHARS]}\n```\n"
    )
    try:
        resolved = chat(system, user).strip()
    except Exception:
        return strip_conflict_markers_prefer_head(conflicted_text)
    if not resolved or CONFLICT_MARKER.search(resolved):
        return strip_conflict_markers_prefer_head(conflicted_text)
    if resolved.startswith("```"):
        resolved = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", resolved)
        resolved = re.sub(r"\n?```$", "", resolved)
    return resolved


def _optional_chat() -> ResolveFn | None:
    """Use OpenAI / Models chat when configured; otherwise None (marker strip)."""
    try:
        from codegen_models import chat_completion, select_provider
    except Exception:
        return None

    def _chat(system: str, user: str) -> str:
        provider, model = select_provider("merge conflict", "resolve conflicts")
        if provider == "cursor":
            # Cursor SDK path is agent-based; fall back to OpenAI/Models JSON chat.
            provider, model = "openai", os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
            if not (os.environ.get("OPENAI_API_KEY") or "").strip():
                provider, model = (
                    "github-models",
                    os.environ.get("GITHUB_MODELS_MODEL") or "openai/gpt-4o-mini",
                )
        return chat_completion(provider=provider, model=model, system=system, user=user)

    return _chat


def merge_default_into_pr_branch(
    repo: str,
    pr: dict[str, Any],
    *,
    work_dir: Path | None = None,
    push: bool = True,
    chat: ResolveFn | None = None,
    recent_brief: str | None = None,
) -> dict[str, Any]:
    """Merge the PR base branch into the PR head and resolve text conflicts.

    Uses a temporary clone so Actions (checked out on ``main``) can still update
    the Builder PR head without inventing a second branch.
    """
    owner, name = split_repo(repo)
    pr_number = int(pr["number"])
    head_ref = (pr.get("head") or {}).get("ref")
    base_ref = (pr.get("base") or {}).get("ref") or "main"
    if not head_ref:
        raise GitHubError(f"PR #{pr_number} missing head.ref")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise GitHubError("missing GITHUB_TOKEN for conflict merge clone")

    brief = recent_brief or summarize_recent_closed_work(repo)
    issue_hint = 0
    title = pr.get("title") or ""
    m = re.search(r"#(\d+)", title) or re.search(r"#(\d+)", pr.get("body") or "")
    if m:
        issue_hint = int(m.group(1))
    resolution_brief = format_conflict_resolution_brief(
        repo, issue_hint or pr_number, pr
    )
    if brief:
        resolution_brief = f"{resolution_brief}\n{brief}"

    chat_fn = chat if chat is not None else _optional_chat()
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"

    with tempfile.TemporaryDirectory(prefix="builder-conflict-") as tmp:
        root = Path(work_dir) if work_dir else Path(tmp) / "repo"
        if work_dir is None:
            _run_git(
                ["clone", "--branch", head_ref, "--single-branch", clone_url, str(root)],
                cwd=Path(tmp),
            )
        _run_git(["config", "user.name", "saberistic-agent-web-builder"], cwd=root)
        _run_git(
            ["config", "user.email", "builder@users.noreply.github.com"],
            cwd=root,
        )
        # --single-branch clones only track the PR head refspec. A plain
        # `git fetch origin main` updates FETCH_HEAD but does NOT create
        # refs/remotes/origin/main, so `merge origin/main` fails with
        # "not something we can merge" and loops Builder↔Reviewer forever.
        _run_git(
            [
                "fetch",
                "origin",
                f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
            ],
            cwd=root,
        )
        merge = _run_git(
            ["merge", f"origin/{base_ref}", "--no-edit"],
            cwd=root,
            check=False,
        )
        if merge.returncode == 0:
            smoke_ok, smoke_detail, repairs = smoke_and_maybe_repair_app(root)
            if not smoke_ok:
                return {
                    "status": "broken_after_resolve",
                    "pr": pr_number,
                    "head": head_ref,
                    "base": base_ref,
                    "conflicted": [],
                    "leftover_markers": [],
                    "smoke_error": smoke_detail,
                    "repairs": repairs,
                    "pushed": False,
                }
            if repairs:
                _run_git(["add", "--", "app/main.py"], cwd=root)
                _run_git(
                    [
                        "commit",
                        "-m",
                        f"builder: repair app.main wiring after merge (#{pr_number})",
                    ],
                    cwd=root,
                    check=False,
                )
            if push:
                _run_git(["push", "origin", f"HEAD:{head_ref}"], cwd=root)
            return {
                "status": "merged_clean",
                "pr": pr_number,
                "head": head_ref,
                "base": base_ref,
                "conflicted": [],
                "repairs": repairs,
            }

        conflicted = list_unmerged_paths(root)
        if not conflicted:
            raise GitHubError(
                f"merge failed without conflict list: {(merge.stderr or merge.stdout or '')[:500]}"
            )

        resolved_paths: list[str] = []
        for rel in conflicted:
            path = root / rel
            if not path.is_file():
                _run_git(["checkout", "--theirs", "--", rel], cwd=root, check=False)
                _run_git(["add", "--", rel], cwd=root, check=False)
                resolved_paths.append(rel)
                continue
            raw = path.read_bytes()
            if b"\0" in raw[:8000] or rel.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico")
            ):
                # Prefer base (recently merged) for binaries when both changed.
                _run_git(["checkout", "--theirs", "--", rel], cwd=root, check=False)
                _run_git(["add", "--", rel], cwd=root)
                resolved_paths.append(rel)
                continue
            text = raw.decode("utf-8", errors="replace")
            fixed = default_resolve_file(
                rel, text, resolution_brief, chat=chat_fn
            )
            path.write_text(fixed, encoding="utf-8")
            _run_git(["add", "--", rel], cwd=root)
            resolved_paths.append(rel)

        _run_git(
            [
                "commit",
                "-m",
                f"builder: merge origin/{base_ref} and resolve conflicts for #{pr_number}",
            ],
            cwd=root,
        )

        # Fail closed: do not push or claim ``resolved`` when markers remain or
        # ``app.main`` no longer imports (classic Builder↔Reviewer loop).
        marker_hits = leftover_conflict_markers(root, resolved_paths)
        smoke_ok, smoke_detail, repairs = smoke_and_maybe_repair_app(root)
        if repairs:
            _run_git(["add", "--", "app/main.py"], cwd=root)
            _run_git(
                [
                    "commit",
                    "-m",
                    f"builder: repair app.main wiring after conflict resolve (#{pr_number})",
                ],
                cwd=root,
                check=False,
            )
        if marker_hits or not smoke_ok:
            return {
                "status": "broken_after_resolve",
                "pr": pr_number,
                "head": head_ref,
                "base": base_ref,
                "conflicted": resolved_paths,
                "leftover_markers": marker_hits,
                "smoke_error": None if smoke_ok else smoke_detail,
                "repairs": repairs,
                "pushed": False,
            }

        if push:
            _run_git(["push", "origin", f"HEAD:{head_ref}"], cwd=root)
        return {
            "status": "resolved",
            "pr": pr_number,
            "head": head_ref,
            "base": base_ref,
            "conflicted": resolved_paths,
            "repairs": repairs,
        }


def maybe_resolve_pr_conflicts(
    repo: str,
    issue: int,
    *,
    force: bool = False,
    push: bool = True,
    chat: ResolveFn | None = None,
) -> dict[str, Any]:
    """If the linked open PR conflicts with its base, merge + resolve using recent work."""
    prs = linked_open_prs(repo, issue)
    if not prs:
        return {"status": "no_pr", "issue": issue}

    pr_number = int(prs[0]["number"])
    pr = refresh_pr(repo, pr_number)
    # mergeable is null until GitHub computes it — one refresh helps.
    if pr.get("mergeable") is None:
        pr = refresh_pr(repo, pr_number)

    needs = pr_needs_conflict_resolution(pr)
    if not needs and not force:
        return {
            "status": "clean",
            "issue": issue,
            "pr": pr_number,
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state"),
        }

    recent = summarize_recent_closed_work(repo)
    post_issue_comment(
        repo,
        issue,
        (
            "### builder_conflict_context\n"
            f"- pr: #{pr_number}\n"
            f"- mergeable: `{pr.get('mergeable')}`\n"
            f"- mergeable_state: `{pr.get('mergeable_state')}`\n"
            f"- head: `{(pr.get('head') or {}).get('ref')}`\n\n"
            f"{recent}\n"
        ),
    )

    result = merge_default_into_pr_branch(
        repo,
        pr,
        push=push,
        chat=chat,
        recent_brief=recent,
    )
    result["issue"] = issue
    lines = [
        "### builder_conflict_result\n",
        f"- status: `{result.get('status')}`\n",
        f"- pr: #{pr_number}\n",
        f"- head: `{result.get('head')}`\n",
        f"- base: `{result.get('base')}`\n",
        f"- conflicted: {', '.join(f'`{p}`' for p in result.get('conflicted') or []) or '(none)'}\n",
    ]
    if result.get("status") == "broken_after_resolve":
        markers = result.get("leftover_markers") or []
        if markers:
            lines.append(
                "- leftover_markers: "
                + ", ".join(f"`{p}`" for p in markers)
                + "\n"
            )
        if result.get("smoke_error"):
            lines.append(f"- smoke_error: `{result['smoke_error']}`\n")
        lines.append(
            "- note: resolution did **not** push; re-enter Builder (`waiting`) — "
            "do not hand off to Reviewer until `from app.main import app` succeeds.\n"
        )
    post_issue_comment(repo, issue, "".join(lines))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--context-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.repo:
        raise SystemExit("--repo or GITHUB_REPOSITORY required")

    if args.context_only:
        print(summarize_recent_closed_work(args.repo))
        return 0

    result = maybe_resolve_pr_conflicts(
        args.repo,
        args.issue,
        force=args.force,
        push=not args.no_push,
    )
    print(result)
    return (
        0
        if result.get("status") in {"clean", "merged_clean", "resolved", "no_pr"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
