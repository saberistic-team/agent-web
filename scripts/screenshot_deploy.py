#!/usr/bin/env python3
"""Capture headless screenshots of the deployed app (pre-merge or post-deploy)."""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from github_api import GitHubError, api, post_issue_comment, split_repo, token

DEFAULT_BASE = "https://agent-web-hello.onrender.com"
PATHS = ("/", "/about")


def wait_healthy(base_url: str, attempts: int = 12) -> None:
    health = f"{base_url.rstrip('/')}/health"
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(health, timeout=20) as resp:
                if 200 <= resp.status < 300:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(10)
    raise GitHubError(f"deploy not healthy at {health}: {last}")


def capture(base_url: str, out_dir: Path, *, phase: str = "pre") -> list[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GitHubError(
            "playwright not installed; pip install playwright && playwright install chromium"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    base = base_url.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for route in PATHS:
            url = f"{base}{route}"
            last_err: Exception | None = None
            for _ in range(6):
                try:
                    page.goto(url, wait_until="networkidle", timeout=60_000)
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    time.sleep(8)
            if last_err is not None:
                raise GitHubError(f"failed to load {url}: {last_err}")
            safe = "home" if route == "/" else route.strip("/").replace("/", "-")
            dest = out_dir / f"{phase}-{safe}.png"
            page.screenshot(path=str(dest), full_page=True)
            paths.append(dest)
        browser.close()
    return paths


def upload_to_branch(
    repo: str, branch: str, files: list[Path], prefix: str
) -> list[str]:
    owner, name = split_repo(repo)
    token()
    urls: list[str] = []
    for path in files:
        rel = f"{prefix}/{path.name}"
        content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        put_body = {
            "message": f"review: screenshot {path.name}",
            "content": content_b64,
            "branch": branch,
        }
        try:
            existing = api("GET", f"/repos/{owner}/{name}/contents/{rel}?ref={branch}")
            put_body["sha"] = existing["sha"]
        except GitHubError:
            pass
        api("PUT", f"/repos/{owner}/{name}/contents/{rel}", body=put_body)
        urls.append(f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{rel}")
    return urls


def comment_markdown(
    heading: str, base_url: str, urls: list[str], extra: list[str] | None = None
) -> str:
    lines = [
        heading,
        f"- deploy: `{base_url}`",
        "- evidence (headless Chromium):",
    ]
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        lines.append(f"  - {name}: ![{name}]({url})")
    if extra:
        lines.extend(extra)
    return "\n".join(lines) + "\n"


def comment_on_issue_or_pr(repo: str, number: int, body: str) -> None:
    post_issue_comment(repo, number, body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--phase", choices=("pre", "post"), default="pre")
    parser.add_argument(
        "--base-url", default=os.environ.get("DEPLOY_BASE_URL", DEFAULT_BASE)
    )
    parser.add_argument("--out-dir", type=Path, default=Path("trace/screenshots"))
    parser.add_argument("--wait-healthy", action="store_true")
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)

    try:
        if args.wait_healthy or args.phase == "post":
            wait_healthy(args.base_url)

        files = capture(args.base_url, args.out_dir, phase=args.phase)
        owner, name = split_repo(args.repo)
        branch = args.branch
        target = args.pr or args.issue
        if not target:
            raise GitHubError("need --pr or --issue to comment")

        if args.pr and not branch:
            pr = api("GET", f"/repos/{owner}/{name}/pulls/{args.pr}")
            branch = pr["head"]["ref"]
        if not branch:
            branch = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"

        prefix = (
            f".agent/screenshots/pr-{args.pr}"
            if args.pr
            else f".agent/screenshots/issue-{args.issue}"
        )
        urls = upload_to_branch(args.repo, branch, files, prefix)
        heading = (
            "### reviewer_screenshots_pre"
            if args.phase == "pre"
            else "### deploy_screenshots_post"
        )
        body = comment_markdown(heading, args.base_url, urls)
        comment_on_issue_or_pr(args.repo, target, body)
        if args.issue and args.pr and args.issue != args.pr:
            comment_on_issue_or_pr(args.repo, args.issue, body)
        print("\n".join(urls))
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
