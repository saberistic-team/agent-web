"""Concrete discovery source adapters."""

from app.discovery.adapters.api import JsonApiAdapter, build_api_adapter
from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.adapters.rss import RssFeedAdapter, build_rss_adapter
from app.discovery.adapters.sitemap import SitemapAdapter, build_sitemap_adapter

__all__ = [
    "DiscoverySourceAdapter",
    "DiscoverySourceRegistry",
    "JsonApiAdapter",
    "RssFeedAdapter",
    "SitemapAdapter",
    "build_api_adapter",
    "build_rss_adapter",
    "build_sitemap_adapter",
]
