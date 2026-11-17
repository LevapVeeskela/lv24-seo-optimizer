"""Yandex OAuth login/logout for the ``auth`` command.

The flow uses Yandex's ``verification_code`` redirect: the user approves the app, Yandex
shows a short code, and the CLI exchanges it for an access token and a refresh token. The
same app can carry ``webmaster:hostinfo`` and ``metrika:read``; the CLI stores the result
under the Webmaster or Metrika provider entry so the two engines stay independent.

Security rules
--------------
* the client secret is read from :class:`~lv24_seo.core.secrets.SecretStore` and is never
  printed, returned or written to the config file;
* the returned payload carries the provider, flow, scopes, expiry and target only — never
  an access or refresh token;
* the authorization URL is printed to stderr so ``--json`` on stdout stays parseable.
"""

from __future__ import annotations

import secrets
import sys
import webbrowser
from typing import Any

import typer

from lv24_seo.core.config import SeoConfig
from lv24_seo.core.errors import AUTH_REQUIRED, CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.core.oauth import is_placeholder_client_id
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.yandex_oauth import (
    CLIENT_SECRET_NAME,
    DEFAULT_SCOPES,
    VERIFICATION_CODE_REDIRECT_URI,
    authorization_url,
    exchange_code,
    provider_scope_name,
    provider_secret_names,
    store_tokens,
)

__all__ = ["login_yandex", "logout_yandex"]

_CLIENT_ID_NEXT_ACTION = (
    "set [auth.yandex] client_id in seo.toml or LV24_SEO_YANDEX_CLIENT_ID, then rerun "
    "`lv24-seo auth login --provider yandex`"
)
_CLIENT_SECRET_NEXT_ACTION = "lv24-seo auth set --provider yandex_oauth_secret --stdin"  # noqa: S105 - a CLI hint
_CODE_NEXT_ACTION = "rerun with --code <code> from https://oauth.yandex.ru/verification_code"


def login_yandex(
    provider: str,
    config: SeoConfig,
    *,
    code: str = "",
    no_browser: bool = False,
    url_only: bool = False,
    store: SecretStore | None = None,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    """Run the Yandex authorization-code flow and store the resulting tokens.

    ``provider`` is ``yandex`` (Webmaster) or ``metrika``. The two entries share one Yandex
    application but keep separate token slots, so revoking one does not break the other.
    """
    name = _provider_name(provider)
    auth = config.auth.yandex
    client_id = auth.client_id.strip()
    if not client_id:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="Yandex OAuth client_id is not configured",
            next_action=_CLIENT_ID_NEXT_ACTION,
        )
    if is_placeholder_client_id(client_id):
        raise SeoError(
            code=CONFIG_INVALID,
            message="Yandex OAuth client_id is a placeholder",
            next_action=(
                "create an app at https://oauth.yandex.ru/ and set [auth.yandex] client_id "
                "or LV24_SEO_YANDEX_CLIENT_ID"
            ),
        )

    resolved_store = store if store is not None else SecretStore(data_dir=config.storage.data_dir)
    client_secret = _client_secret(resolved_store)
    scopes = _scopes(auth.scopes)
    redirect_uri = auth.redirect_uri.strip() or VERIFICATION_CODE_REDIRECT_URI
    url = authorization_url(
        client_id,
        scopes,
        secrets.token_urlsafe(24),
        redirect_uri=redirect_uri,
    )

    if url_only:
        # Desktop/web clients need the URL without starting a blocking code prompt.
        return {
            "provider": name,
            "flow": "oauth",
            "authorization_url": url,
            "scopes": list(scopes),
            "target": _target(name, config),
        }

    if no_browser:
        typer.echo(url, err=True)
    elif not webbrowser.open(url):
        # A headless host cannot open a browser; showing the URL is the only way forward.
        typer.echo(url, err=True)

    supplied = code.strip()
    if not supplied:
        if not sys.stdin.isatty():
            raise SeoError(
                code=INVALID_INPUT,
                message="no Yandex authorization code was provided",
                next_action=_CODE_NEXT_ACTION,
            )
        typer.echo(
            "Approve access in the browser, then paste the code shown at "
            f"{VERIFICATION_CODE_REDIRECT_URI}.",
            err=True,
        )
        supplied = typer.prompt("Authorization code", hide_input=True, err=True).strip()
        if not supplied:
            raise SeoError(
                code=INVALID_INPUT,
                message="no Yandex authorization code was provided",
                next_action=_CODE_NEXT_ACTION,
            )

    tokens = exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=supplied,
        redirect_uri=redirect_uri,
        provider=name,
        http=http,
    )
    store_tokens(resolved_store, name, tokens, keep_refresh=False, requested_scopes=scopes)

    payload: dict[str, Any] = {
        "provider": name,
        "flow": "oauth",
        "scopes": scopes,
        "expires_at": (
            tokens.expires_at.isoformat(timespec="seconds")
            if tokens.expires_at is not None
            else None
        ),
        "target": _target(name, config),
    }
    return payload


def logout_yandex(store: SecretStore, provider: str) -> None:
    """Remove the provider's access token, refresh token, scopes and expiry; keep the app secret."""
    for name in (*provider_secret_names(provider), provider_scope_name(provider)):
        store.delete(name)


def _provider_name(provider: str) -> str:
    name = provider.strip().lower()
    if name not in {"yandex", "metrika"}:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown Yandex OAuth provider {provider!r}",
            next_action="use `--provider yandex` or `--provider metrika`",
        )
    return name


def _client_secret(store: SecretStore) -> str:
    value = store.get(CLIENT_SECRET_NAME)
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="Yandex OAuth client secret is not configured",
            next_action=_CLIENT_SECRET_NEXT_ACTION,
        )
    return text


def _scopes(configured: list[str]) -> list[str]:
    scopes = [scope.strip() for scope in configured if scope.strip()]
    return scopes or list(DEFAULT_SCOPES)


def _target(provider: str, config: SeoConfig) -> str:
    if provider == "yandex":
        webmaster = config.providers.yandex_webmaster
        return webmaster.host_id if webmaster is not None else ""
    metrika = config.providers.yandex_metrika
    return metrika.counter if metrika is not None else ""
