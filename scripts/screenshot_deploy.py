#!/usr/bin/env python3
"""Capture headless screenshots of the deployed app and attach them to a PR."""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

from github_api import GitHubError, api, post_issue_comment, split_repo, token


DEFAULT_BASE = "https://agent-web-hello.onrender.com"
PATHS = ("/", "/about")


def capture(base_url: str, out_dir: Path) -> list[Path]:
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
            # Cold-start retries for free Render
            last_err: Exception | None = None
            for attempt in range(6):
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
            dest = out_dir / f"{safe}.png"
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
        urls.append(
            f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{rel}"
        )
    return urls


def comment_on_pr(repo: str, pr_number: int, urls: list[str], base_url: str) -> None:
    lines = [
        "### reviewer_screenshots",
        f"- deploy: `{base_url}`",
        "- evidence (headless Chromium):",
    ]
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        lines.append(f"  - {name}: ![{name}]({url})")
    # PR comments use issues API with PR number
    post_issue_comment(repo, pr_number, "\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--base-url", default=os.environ.get("DEPLOY_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--out-dir", type=Path, default=Path("trace/screenshots"))
    args = parser.parse_args(argv)

    try:
        owner, name = split_repo(args.repo)
        pr = api("GET", f"/repos/{owner}/{name}/pulls/{args.pr}")
        branch = pr["head"]["ref"]
        files = capture(args.base_url, args.out_dir)
        prefix = f".agent/screenshots/pr-{args.pr}"
        urls = upload_to_branch(args.repo, branch, files, prefix)
        comment_on_pr(args.repo, args.pr, urls, args.base_url)
        print("\n".join(urls))
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
