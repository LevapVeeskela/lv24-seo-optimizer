"""Bing Webmaster Tools + IndexNow adapter (see ``docs/data-sources.md`` §4)."""

from lv24_seo.providers.bing.client import (
    BingClient,
    indexnow_submit,
    validate_indexnow_key,
    validate_indexnow_key_location,
)

__all__ = [
    "BingClient",
    "indexnow_submit",
    "validate_indexnow_key",
    "validate_indexnow_key_location",
]
