"""Yandex OAuth connector for Webmaster and Metrika.

The real authorization-code/refresh flow is implemented in this module. The stub keeps
the CLI importable while the connector is being built; it never claims success.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.core.config import SeoConfig
from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.secrets import SecretStore


def login_yandex(
    provider: str,
    config: SeoConfig,
    *,
    code: str = "",
    no_browser: bool = False,
) -> dict[str, Any]:
    """Run the Yandex OAuth flow and store the tokens (not implemented yet)."""
    raise SeoError(
        code=INVALID_INPUT,
        message=f"{provider} OAuth connector is not implemented yet",
        next_action="use `lv24-seo auth login --provider yandex --manual`",
    )


def logout_yandex(store: SecretStore, provider: str) -> None:
    """Remove the Yandex OAuth access/refresh tokens and expiry."""
    return None
