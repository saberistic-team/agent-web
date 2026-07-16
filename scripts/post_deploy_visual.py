#!/usr/bin/env python3
"""After deploy: capture post screenshots and ask Cursor (preferred) / OpenAI
whether the issue change is visible.
"""

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
from typing import Any

from cursor_model import DEFAULT_CURSOR_MODEL, cursor_model_dict, cursor_model_selection
from github_api import (
    GitHubError,
    api,
    create_branch,
    enable_auto_merge,
    find_open_pr_for_branch,
    open_pull_request,
    post_issue_comment,
    split_repo,
)
from screenshot_deploy import (
    PRE_BRANCH_PHASE,
    capture,
    fetch_pr_changed_paths,
    resolve_base_url,
    resolve_screenshot_routes,
    upload_to_branch,
    wait_healthy,
    comment_markdown,
)

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

RECORD_COMMIT_PREFIX = "deploy: record post-deploy artifacts"


def record_branch_name(sha: str) -> str:
    """Deterministic per-deploy branch so a rerun for the same deploy reuses
    one branch/PR instead of opening a duplicate (mirrors
    ``freeze_shipped_migrations.freeze_branch_name``)."""
    short = (sha or "local")[:12] or "local"
    return f"deploy/screenshots-{short}"


def record_pr_body(short: str, base_url: str) -> str:
    return (
        "### deploy_record\n"
        f"- deploy: `{base_url}`\n"
        f"- sha: `{short}`\n"
        "- Automated, evidence-only change: records this deploy's `/health` "
        "snapshot and screenshot uploads under `.agent/`. No application "
        "code, migration, or test changes.\n"
        "- Opened as a PR (not a direct push) because the workflow-governance "
        "ruleset requires every change to `main` go through review — see "
        "`docs/WORKFLOW_GOVERNANCE.md`. Auto-merge is enabled: approving this "
        "PR is sufficient, no separate merge click needed.\n"
    )


def open_or_reuse_record_pr(
    repo: str,
    head_branch: str,
    base_branch: str,
    *,
    short: str,
    base_url: str,
) -> dict[str, Any]:
    """Open (or reuse) the auto-merge PR that lands this deploy's recorded
    evidence on ``base_branch``.

    Same pattern as ``freeze_shipped_migrations.maybe_commit_freeze``: a
    direct push to a protected branch is rejected by the workflow-governance
    ruleset (issue #362), so evidence commits land on a dedicated branch and
    merge themselves via GitHub's native auto-merge the instant a human
    CODEOWNER approves. Reruns against the same deploy sha reuse the existing
    open PR instead of opening a duplicate.
    """
    existing = find_open_pr_for_branch(repo, head_branch)
    if existing is not None:
        return {"number": existing["number"], "url": existing["html_url"]}
    title = f"{RECORD_COMMIT_PREFIX} ({short})"
    pr = open_pull_request(
        repo,
        head=head_branch,
        base=base_branch,
        title=title,
        body=record_pr_body(short, base_url),
    )
    enable_auto_merge(repo, pr["node_id"])
    return {"number": pr["number"], "url": pr["html_url"]}


def record_health(
    repo: str,
    branch: str,
    *,
    sha: str,
    base_url: str,
    health: dict,
) -> dict[str, str]:
    """Persist /health JSON after every deploy (file on branch + optional summary)."""
    short = (sha or "local")[:12] or "local"
    slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
    payload = {
        "sha": sha or short,
        "base_url": base_url,
        "health_url": health.get("_health_url") or f"{base_url.rstrip('/')}/health",
        "health": slim,
    }
    out = Path("trace/deploy-health.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")

    prefix = f".agent/deploy/{short}"
    urls = upload_to_branch(
        repo, branch, [out], prefix, message=f"deploy: record health ({short})"
    )
    raw_url = urls[0] if urls else ""

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Deploy health\n\n")
            fh.write(f"- base: `{base_url}`\n")
            fh.write(f"- sha: `{sha or short}`\n")
            fh.write(f"- value: `{json.dumps(slim, separators=(',', ':'))}`\n")
            if raw_url:
                fh.write(f"- recorded: {raw_url}\n")

    print(f"deploy_health={json.dumps(slim, separators=(',', ':'))}")
    if raw_url:
        print(f"deploy_health_url={raw_url}")
    return {"path": f"{prefix}/deploy-health.json", "url": raw_url, "json": json.dumps(slim)}


def cursor_api_key() -> str | None:
    value = os.environ.get("CURSOR_API_KEY")
    return value.strip() if value and value.strip() else None


def openai_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY")
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


def find_issue_from_commit(repo: str, sha: str) -> int | None:
    """Resolve issue via PRs associated with the commit (merge or squash)."""
    if not sha:
        return None
    owner, name = split_repo(repo)
    try:
        prs = api("GET", f"/repos/{owner}/{name}/commits/{sha}/pulls") or []
    except GitHubError:
        return None
    if not isinstance(prs, list):
        return None
    for pr in prs:
        blob = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        found = find_issue_number(blob)
        if found:
            return found
    return None


def list_branch_pre_urls(repo: str, ref: str, pr: int | None) -> list[str]:
    """Return PR-branch pre-merge preview shot URLs (``branch-*.png``)."""
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
        name_part = path.rsplit("/", 1)[-1]
        if name_part.startswith(f"{PRE_BRANCH_PHASE}-") and name_part.endswith(".png"):
            # Prefer desktop public shots for visual compare; skip admin on prod compare.
            if "-admin" in name_part:
                continue
            urls.append(
                f"https://raw.githubusercontent.com/{owner}/{name}/{ref}/{path}"
            )
    return urls


def _parse_visual_json(text: str, *, model: str, provider: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise GitHubError(f"{provider} visual did not return JSON: {text[:400]!r}")
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise GitHubError(f"{provider} visual JSON root must be object")
    decision = str(data.get("decision") or "fail").lower()
    if decision not in {"pass", "fail", "skip"}:
        decision = "fail"
    return {
        "visible": data.get("visible"),
        "summary": str(data.get("summary") or text[:500]),
        "decision": decision,
        "model": model,
        "provider": provider,
    }


def _visual_prompt(
    issue_title: str,
    issue_body: str,
    pre_paths: list[Path],
    post_paths: list[Path],
) -> str:
    pre_list = "\n".join(f"- {p.resolve()}" for p in pre_paths) or "- (none)"
    post_list = "\n".join(f"- {p.resolve()}" for p in post_paths) or "- (none)"
    return (
        "READ-ONLY VISUAL CHECK. Do not create, edit, delete, or move any files. "
        "Do not run mutating shell/git commands. Do not open PRs.\n"
        "Open the screenshot PNG paths below (Read tool) and compare before vs after.\n"
        "Respond with JSON only (no prose outside JSON):\n"
        '{"visible": boolean, "summary": "string", "decision": "pass"|"fail"}\n'
        "pass if the post screenshots clearly show the issue change; "
        "fail if unchanged or unrelated.\n\n"
        f"Issue title: {issue_title}\n"
        f"Issue body:\n{(issue_body or '')[:4000]}\n\n"
        f"BEFORE screenshot paths:\n{pre_list}\n\n"
        f"AFTER screenshot paths:\n{post_list}\n"
    )


def visual_ai_check_cursor(
    *,
    issue_title: str,
    issue_body: str,
    pre_paths: list[Path],
    post_paths: list[Path],
) -> dict:
    key = cursor_api_key()
    if not key:
        raise GitHubError("missing CURSOR_API_KEY")
    model = os.environ.get("CURSOR_MODEL") or DEFAULT_CURSOR_MODEL
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
        from cursor_sdk_patch import patch_callback_auth_tokens

        patch_callback_auth_tokens()
    except ImportError as exc:
        raise GitHubError(
            "cursor-sdk is not installed; pip install -r requirements-agents.txt"
        ) from exc

    prompt = _visual_prompt(issue_title, issue_body, pre_paths, post_paths)
    try:
        result = Agent.prompt(
            prompt,
            AgentOptions(
                model=cursor_model_selection(model),
                api_key=key,
                name="post-deploy-visual",
                mode="plan",
                local=LocalAgentOptions(cwd=os.getcwd()),
            ),
        )
    except TypeError:
        try:
            result = Agent.prompt(
                prompt,
                {
                    "model": cursor_model_dict(model),
                    "apiKey": key,
                    "name": "post-deploy-visual",
                    "mode": "plan",
                    "local": {"cwd": os.getcwd()},
                },
            )
        except Exception as exc:
            raise GitHubError(f"Cursor visual failed: {exc}") from exc
    except Exception as exc:
        raise GitHubError(f"Cursor visual failed: {exc}") from exc

    status = getattr(result, "status", None)
    text = (getattr(result, "result", None) or "").strip()
    if status != "finished":
        raise GitHubError(
            f"Cursor visual run status={status!r} "
            f"agent_id={getattr(result, 'agent_id', '')} "
            f"result={text[:400]!r}"
        )
    if not text:
        raise GitHubError("Cursor visual returned empty content")
    return _parse_visual_json(text, model=model, provider="cursor")


def visual_ai_check_openai(
    *,
    issue_title: str,
    issue_body: str,
    pre_paths: list[Path],
    post_paths: list[Path],
) -> dict:
    """OpenAI vision backup when Cursor is unavailable."""
    key = openai_key()
    if not key:
        raise GitHubError("missing OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "You compare before/after screenshots of a deployed website.\n"
                "Return ONLY JSON: "
                '{"visible": boolean, "summary": "string", "decision": "pass"|"fail"}.\n'
                "pass if the post screenshots clearly show the issue change; "
                "fail if unchanged or unrelated.\n\n"
                f"Issue title: {issue_title}\n"
                f"Issue body:\n{(issue_body or '')[:4000]}\n"
            ),
        }
    ]
    for label, paths in (("BEFORE", pre_paths), ("AFTER", post_paths)):
        content.append({"type": "text", "text": f"\n{label} screenshots follow."})
        for p in paths:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            )
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "agent-web-visual",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"OpenAI visual -> {exc.code}: {detail}") from exc
    choices = body.get("choices") or []
    out_text = ""
    if choices:
        out_text = str((choices[0].get("message") or {}).get("content") or "").strip()
    return _parse_visual_json(out_text, model=model, provider="openai")


def visual_ai_check(
    *,
    issue_title: str,
    issue_body: str,
    pre_paths: list[Path],
    post_paths: list[Path],
) -> dict:
    """Compare before/after screenshots: Cursor preferred, OpenAI backup."""
    force = (os.environ.get("VISUAL_PROVIDER") or "").strip().lower()
    errors: list[str] = []

    def _try_cursor() -> dict | None:
        if not cursor_api_key():
            return None
        try:
            return visual_ai_check_cursor(
                issue_title=issue_title,
                issue_body=issue_body,
                pre_paths=pre_paths,
                post_paths=post_paths,
            )
        except Exception as exc:
            errors.append(f"cursor: {exc}")
            return None

    def _try_openai() -> dict | None:
        if not openai_key():
            return None
        try:
            return visual_ai_check_openai(
                issue_title=issue_title,
                issue_body=issue_body,
                pre_paths=pre_paths,
                post_paths=post_paths,
            )
        except Exception as exc:
            errors.append(f"openai: {exc}")
            return None

    if force in {"cursor", "cursor-sdk", "composer"}:
        order = ["cursor", "openai"]
    elif force in {"openai", "chatgpt"}:
        order = ["openai", "cursor"]
    else:
        order = ["cursor", "openai"]

    for name in order:
        got = _try_cursor() if name == "cursor" else _try_openai()
        if got is not None:
            return got

    if not cursor_api_key() and not openai_key():
        return {
            "visible": None,
            "summary": "CURSOR_API_KEY and OPENAI_API_KEY missing; skipped visual AI check",
            "decision": "skip",
            "model": "",
            "provider": "none",
        }
    raise GitHubError(
        "visual AI check failed: " + " | ".join(errors or ["no providers"])
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--base-url", default="")
    args = parser.parse_args(argv)

    try:
        owner, name = split_repo(args.repo)
        issue_num = (
            args.issue
            or find_issue_number(args.commit_message)
            or find_issue_from_commit(args.repo, args.sha)
        )
        default = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"
        short = (args.sha or "local")[:12] or "local"
        # Record evidence on a dedicated branch + auto-merge PR rather than
        # pushing straight to the default branch — same reason and pattern as
        # freeze_shipped_migrations.py's maybe_commit_freeze (issue #362): the
        # workflow-governance ruleset rejects direct bot pushes to a
        # protected branch with a 422.
        record_branch = record_branch_name(args.sha)
        create_branch(args.repo, record_branch, base_branch=default)
        base_url = resolve_base_url(args.base_url)
        health = wait_healthy(base_url)
        health_slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
        health_rec = record_health(
            args.repo,
            record_branch,
            sha=args.sha,
            base_url=base_url,
            health=health,
        )

        out = Path("trace/screenshots-post")
        changed: list[str] | None = None
        if args.pr:
            changed = fetch_pr_changed_paths(args.repo, args.pr)
        elif args.sha:
            # Resolve merged PR(s) for this deploy SHA and union their files.
            try:
                prs = api("GET", f"/repos/{owner}/{name}/commits/{args.sha}/pulls") or []
            except GitHubError:
                prs = []
            paths: list[str] = []
            for pr in prs if isinstance(prs, list) else []:
                num = pr.get("number")
                if num:
                    paths.extend(fetch_pr_changed_paths(args.repo, int(num)))
            changed = paths or None
        routes = resolve_screenshot_routes(
            changed_files=changed, include_admin=False
        )
        if not routes and changed is not None:
            post_files: list = []
            post_urls: list[str] = []
        else:
            post_files = capture(
                base_url,
                out,
                phase="post",
                routes=routes if changed is not None else None,
                allow_admin=False,
            ).paths
            prefix = (
                f".agent/screenshots/issue-{issue_num}/post"
                if issue_num
                else f".agent/screenshots/deploy-{short}/post"
            )
            post_urls = (
                upload_to_branch(args.repo, record_branch, post_files, prefix)
                if post_files
                else []
            )

        health_line = (
            f"- health: `{json.dumps(health_slim, separators=(',', ':'))}`"
            + (f" ([recorded]({health_rec['url']}))" if health_rec.get("url") else "")
        )

        if not issue_num:
            record_pr = open_or_reuse_record_pr(
                args.repo, record_branch, default, short=short, base_url=base_url
            )
            print(
                "No issue number in commit message / linked PR; "
                f"uploaded post screenshots: {post_urls}; {health_line}"
            )
            print(
                "Tip: include `Closes #N` or `(#N)` in the commit/PR body "
                "so Reviewer gets deploy_visual_check on the issue."
            )
            print(f"record_pr={record_pr['url']}")
            return 0

        issue = api("GET", f"/repos/{owner}/{name}/issues/{issue_num}")

        # Before shots are PR-branch previews (no saberistic.com pre-merge).
        pre_files = sorted(
            p
            for p in Path("trace/screenshots").glob("branch-*.png")
            if "-admin" not in p.name
        )
        pre_urls = (
            list_branch_pre_urls(args.repo, default, args.pr) if not pre_files else []
        )
        if pre_files:
            pre_urls = upload_to_branch(
                args.repo,
                record_branch,
                pre_files,
                f".agent/screenshots/issue-{issue_num}/pre",
            )

        if not post_files and changed is not None:
            visual = {
                "visible": None,
                "summary": "No public pages affected by merge; visual check skipped",
                "decision": "skip",
                "model": "",
                "provider": "none",
            }
            body = (
                "### deploy_visual_check\n"
                f"- deploy: `{base_url}`\n"
                "- phase: `post-deploy`\n"
                f"- issue: #{issue_num}\n"
                f"{health_line}\n"
                "- routes (public, PR-affected): (none)\n"
                "- note: no public pages affected; screenshots skipped\n"
                f"- visual_decision: `{visual['decision']}`\n"
                f"- visual_summary: {visual['summary']}\n"
            )
            post_issue_comment(args.repo, issue_num, body)
        else:
            visual = visual_ai_check(
                issue_title=issue.get("title") or "",
                issue_body=issue.get("body") or "",
                pre_paths=pre_files,
                post_paths=post_files,
            )

            extra = [
                "- phase: `post-deploy`",
                f"- issue: #{issue_num}",
                health_line,
                f"- visual_provider: `{visual.get('provider')}`",
                f"- visual_decision: `{visual.get('decision')}`",
                f"- visual_visible: `{visual.get('visible')}`",
                f"- visual_model: `{visual.get('model')}`",
                f"- visual_summary: {visual.get('summary')}",
            ]
            if routes and changed is not None:
                extra.insert(
                    2,
                    "- routes (public, PR-affected): "
                    + ", ".join(f"`{r}`" for r in routes),
                )
            body = comment_markdown(
                "### deploy_visual_check",
                base_url,
                post_urls,
                extra=extra,
            )
            if pre_urls:
                pre_lines = ["\n#### Pre-merge branch screenshots"]
                for u in pre_urls:
                    name = u.rsplit("/", 1)[-1]
                    pre_lines.append(f"- **{name}**")
                    pre_lines.append(f"  ![]({u})")
                body += "\n".join(pre_lines) + "\n"
            post_issue_comment(args.repo, issue_num, body)

        try:
            from acceptance import (
                post_checklist,
                update_issue_checkboxes,
                verify_acceptance,
            )

            acceptance = verify_acceptance(
                args.repo, issue_num, args.pr or None, use_ai=True
            )
            post_checklist(args.repo, issue_num, acceptance, role="post-deploy")
            if acceptance.get("all_done"):
                update_issue_checkboxes(args.repo, issue_num, acceptance)
        except Exception as acc_exc:
            post_issue_comment(
                args.repo,
                issue_num,
                f"### acceptance_checklist\n- role: `post-deploy`\n"
                f"- all_done: `false`\n- note: refresh failed (`{acc_exc}`)\n",
            )

        record_pr = open_or_reuse_record_pr(
            args.repo, record_branch, default, short=short, base_url=base_url
        )

        if visual.get("decision") == "fail":
            post_issue_comment(
                args.repo,
                issue_num,
                "@human-review Post-deploy visual check failed — change may not be visible on production.\n",
            )
            print(f"record_pr={record_pr['url']}", file=sys.stderr)
            print("visual check failed", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "issue": issue_num,
                    "visual": visual,
                    "post": post_urls,
                    "record_pr": record_pr["url"],
                }
            )
        )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
