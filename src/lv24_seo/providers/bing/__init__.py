"""Bing Webmaster Tools + IndexNow adapter (see ``docs/data-sources.md`` §4)."""

from lv24_seo.providers.bing.client import BingClient, indexnow_submit

__all__ = ["BingClient", "indexnow_submit"]
