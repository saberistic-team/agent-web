#!/usr/bin/env python3
"""Capture headless screenshots of the deployed app (pre-merge or post-deploy)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from github_api import GitHubError, api, post_issue_comment, split_repo, token

DEFAULT_BASE = "https://agent-web-hello.onrender.com"
# Only HTML pages — never screenshot JSON APIs like /health or /hello.
HTML_PATHS = ("/", "/about")
PATHS = HTML_PATHS  # alias for callers


def resolve_base_url(value: str | None = None) -> str:
    """Prefer explicit value, then DEPLOY_BASE_URL, then default.

    Empty Actions variables (`DEPLOY_BASE_URL:`) must not win over the default.
    """
    for candidate in (value, os.environ.get("DEPLOY_BASE_URL"), DEFAULT_BASE):
        if candidate and str(candidate).strip():
            return str(candidate).strip().rstrip("/")
    return DEFAULT_BASE


def wait_healthy(base_url: str | None = None, attempts: int = 12) -> dict[str, Any]:
    """Poll GET /health until 2xx; return parsed JSON (or raw text wrapper).

    Does not screenshot /health — JSON APIs are evidence text only.
    """
    base = resolve_base_url(base_url)
    health_url = urljoin(base + "/", "health")
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(health_url, timeout=20) as resp:
                if not (200 <= resp.status < 300):
                    raise GitHubError(f"health status {resp.status}")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", errors="replace")
                if "json" in ctype or raw.lstrip().startswith("{"):
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"raw": raw[:500]}
                else:
                    data = {"raw": raw[:500]}
                if isinstance(data, dict):
                    data["_health_url"] = health_url
                return data if isinstance(data, dict) else {"value": data, "_health_url": health_url}
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(10)
    raise GitHubError(f"deploy not healthy at {health_url}: {last}")


def _is_html_response(url: str) -> bool:
    """Return True if the URL serves HTML (skip JSON API routes)."""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "agent-web-screenshots"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" in ctype:
                return True
            # Peek body for doctype/html if content-type is missing/wrong.
            chunk = resp.read(256).decode("utf-8", errors="replace").lstrip().lower()
            return chunk.startswith("<!doctype html") or chunk.startswith("<html")
    except Exception:
        return False


def capture(base_url: str | None, out_dir: Path, *, phase: str = "pre") -> list[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GitHubError(
            "playwright not installed; pip install playwright && playwright install chromium"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    base = resolve_base_url(base_url)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for route in HTML_PATHS:
            url = urljoin(base + "/", route.lstrip("/")) if route != "/" else base + "/"
            if not _is_html_response(url):
                # JSON or non-HTML — skip screenshot (e.g. misconfigured /about).
                continue
            last_err: Exception | None = None
            for _ in range(6):
                try:
                    page.goto(url, wait_until="networkidle", timeout=60_000)
                    # Double-check we did not land on JSON.
                    body_prefix = page.content()[:200].lstrip().lower()
                    if body_prefix.startswith("{") or body_prefix.startswith("["):
                        last_err = GitHubError(f"{url} returned JSON, not HTML")
                        break
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
    if not paths:
        raise GitHubError(
            f"no HTML pages to screenshot under {base} (tried {', '.join(HTML_PATHS)})"
        )
    return paths


def upload_to_branch(
    repo: str, branch: str, files: list[Path], prefix: str, *, message: str | None = None
) -> list[str]:
    owner, name = split_repo(repo)
    token()
    urls: list[str] = []
    for path in files:
        rel = f"{prefix}/{path.name}"
        content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        put_body = {
            "message": message or f"review: record {path.name}",
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
    parser.add_argument("--base-url", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("trace/screenshots"))
    parser.add_argument("--wait-healthy", action="store_true")
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)

    try:
        base_url = resolve_base_url(args.base_url)
        health: dict[str, Any] | None = None
        if args.wait_healthy or args.phase == "post":
            health = wait_healthy(base_url)

        files = capture(base_url, args.out_dir, phase=args.phase)
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
        extra = None
        if health is not None:
            slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
            extra = [f"- health: `{json.dumps(slim, separators=(',', ':'))}`"]
        body = comment_markdown(heading, base_url, urls, extra=extra)
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
