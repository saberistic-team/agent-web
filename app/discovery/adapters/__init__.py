"""Concrete discovery source adapters."""

from app.discovery.adapters.api import JsonApiAdapter, build_api_adapter
from app.discovery.adapters.github import GithubSearchAdapter, build_github_adapter
from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.adapters.rss import RssFeedAdapter, build_rss_adapter
from app.discovery.adapters.sitemap import SitemapAdapter, build_sitemap_adapter
from app.discovery.adapters.yc import YCombinatorAdapter, build_yc_adapter

__all__ = [
    "DiscoverySourceAdapter",
    "DiscoverySourceRegistry",
    "GithubSearchAdapter",
    "JsonApiAdapter",
    "RssFeedAdapter",
    "SitemapAdapter",
    "YCombinatorAdapter",
    "build_api_adapter",
    "build_github_adapter",
    "build_rss_adapter",
    "build_sitemap_adapter",
    "build_yc_adapter",
]
