"""Technical SEO helpers: canonical URLs, robots, sitemap, legacy redirects."""

from __future__ import annotations

from datetime import date

CANONICAL_SCHEME = "https"
CANONICAL_HOST = "saberistic.com"
CANONICAL_BASE = f"{CANONICAL_SCHEME}://{CANONICAL_HOST}"

# HTML pages that return 200 and should be indexed.
INDEXABLE_PATHS: tuple[str, ...] = ("/", "/about", "/brief")

# Permanent redirects for retired marketing URLs.
LEGACY_REDIRECTS: dict[str, str] = {
    "/what-we-do.html": "/#services",
    "/what-we-did.html": "/#work",
    "/who-we-are.html": "/about",
}


def canonical_url(path: str) -> str:
    """Return the apex canonical URL for a site path."""
    if path == "/":
        return f"{CANONICAL_BASE}/"
    return f"{CANONICAL_BASE}{path}"


def robots_txt() -> str:
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {CANONICAL_BASE}/sitemap.xml\n"
    )


def sitemap_xml(*, lastmod: date | None = None) -> str:
    mod = (lastmod or date.today()).isoformat()
    urls = "\n".join(
        "  <url>\n"
        f"    <loc>{canonical_url(path)}</loc>\n"
        f"    <lastmod>{mod}</lastmod>\n"
        "  </url>"
        for path in INDEXABLE_PATHS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def is_www_host(host: str) -> bool:
    hostname = host.split(":")[0].lower()
    return hostname == f"www.{CANONICAL_HOST}"


def apex_redirect_url(path: str, query: str) -> str:
    url = canonical_url(path)
    if query:
        url = f"{url}?{query}"
    return url


def wants_json_not_found(request_path: str, accept: str) -> bool:
    if request_path.startswith(("/api/", "/webhooks/")):
        return True
    lowered = accept.lower()
    if "application/json" in lowered and "text/html" not in lowered:
        return True
    return False
