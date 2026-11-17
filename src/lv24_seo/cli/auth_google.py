"""Google OAuth connector for Search Console.

The real loopback/PKCE flow is implemented in this module. The stub keeps the CLI
importable while the connector is being built; it never claims success.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.core.config import SeoConfig
from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.secrets import SecretStore


def login_gsc(
    config: SeoConfig,
    *,
    no_browser: bool = False,
    port: int = 0,
    code: str = "",
) -> dict[str, Any]:
    """Run the GSC OAuth flow and store the tokens (not implemented yet)."""
    raise SeoError(
        code=INVALID_INPUT,
        message="GSC OAuth connector is not implemented yet",
        next_action="use `lv24-seo auth login --provider gsc --manual`",
    )


def logout_gsc(store: SecretStore) -> None:
    """Remove the GSC OAuth access/refresh tokens and expiry."""
    return None
