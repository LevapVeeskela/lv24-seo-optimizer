"""Google OAuth connector for Search Console.

Implements the installed-app flow: a Desktop OAuth client, PKCE, a loopback callback on
``127.0.0.1`` and ``access_type=offline``. ``auth login --provider gsc`` reaches this module
only when ``[auth.gsc].client_id`` is configured; the client secret is read from the secret
store and never from the config file or argv.

Two entry paths share one exchange:

* the default loopback flow starts :class:`~lv24_seo.core.oauth.LoopbackServer`, prints (and
  usually opens) the consent URL and waits for the code;
* ``--code`` skips the browser entirely and exchanges a code obtained elsewhere, reusing the
  PKCE verifier that the earlier ``--no-browser`` attempt stored.

Neither path returns, prints or logs a token.
"""

from __future__ import annotations

import webbrowser
from datetime import UTC, timedelta
from typing import Any

import typer

from lv24_seo.core.config import OAuthClientConfig, SeoConfig
from lv24_seo.core.errors import AUTH_REQUIRED, CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.oauth import (
    DEFAULT_TIMEOUT_SECONDS,
    LoopbackServer,
    TokenResponse,
    exchange_code,
    generate_pkce,
    generate_state,
    is_placeholder_client_id,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.gsc.auth import WEBMASTERS_READONLY_SCOPE
from lv24_seo.providers.gsc.oauth import (
    ACCESS_TOKEN_SECRET,
    CLIENT_SECRET_NAME,
    CODE_VERIFIER_SECRET,
    EXPIRES_AT_SECRET,
    REFRESH_TOKEN_SECRET,
    SCOPE_SECRET,
    TOKEN_ENDPOINT,
    authorization_url,
)

__all__ = ["login_gsc", "logout_gsc"]

CLIENT_SECRET_NEXT_ACTION = "lv24-seo auth set --provider gsc_oauth_secret --stdin"  # noqa: S105 - a command
CLIENT_ID_NEXT_ACTION = (
    "set [auth.gsc] client_id in seo.toml or LV24_SEO_GSC_CLIENT_ID to the Desktop client id"
)


def login_gsc(
    config: SeoConfig,
    *,
    no_browser: bool = False,
    port: int = 0,
    code: str = "",
) -> dict[str, Any]:
    """Run the GSC OAuth flow, store the tokens and return a token-free summary."""
    client = config.auth.gsc
    client_id = client.client_id.strip()
    if not client_id:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="Google Search Console OAuth client_id is not configured",
            next_action=CLIENT_ID_NEXT_ACTION,
        )
    if is_placeholder_client_id(client_id):
        raise SeoError(
            code=CONFIG_INVALID,
            message="Google Search Console OAuth client_id is a placeholder",
            next_action=(
                "create a Desktop app OAuth client in Google Cloud and set [auth.gsc] client_id "
                "or LV24_SEO_GSC_CLIENT_ID"
            ),
        )

    store = SecretStore(data_dir=config.storage.data_dir)
    client_secret = _stored(store, CLIENT_SECRET_NAME)
    if not client_secret:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="Google Search Console OAuth client secret is missing",
            next_action=CLIENT_SECRET_NEXT_ACTION,
        )

    scopes = tuple(scope.strip() for scope in client.scopes if scope.strip())
    if not scopes:
        scopes = (WEBMASTERS_READONLY_SCOPE,)

    if code.strip():
        return _complete_login(
            config,
            store,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            code=code.strip(),
            redirect_uri=_manual_redirect_uri(client, port),
            code_verifier=_stored(store, CODE_VERIFIER_SECRET),
        )

    verifier, challenge = generate_pkce()
    state = generate_state()
    server = LoopbackServer(state=state, port=port, timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        redirect_uri = server.redirect_uri
        url = authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=challenge,
        )
        # Stored before the wait so a timed-out attempt can still be finished with --code.
        store.set(CODE_VERIFIER_SECRET, verifier)
        server.start()
        # Guidance goes to stderr so `--json` keeps exactly one JSON document on stdout.
        typer.echo(
            "Open this URL in a browser on this machine to authorize Search Console:",
            err=True,
        )
        typer.echo(url, err=True)
        if no_browser:
            typer.echo(f"Waiting for the callback on {redirect_uri} ...", err=True)
        elif not webbrowser.open(url):
            typer.echo("Could not open a browser automatically; open the URL above.", err=True)
        received = server.wait()
    finally:
        server.close()

    return _complete_login(
        config,
        store,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        code=received,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )


def logout_gsc(store: SecretStore) -> None:
    """Remove the GSC OAuth tokens, scopes and expiry; the OAuth client secret is kept."""
    for name in (
        ACCESS_TOKEN_SECRET,
        REFRESH_TOKEN_SECRET,
        EXPIRES_AT_SECRET,
        SCOPE_SECRET,
        CODE_VERIFIER_SECRET,
    ):
        store.delete(name)


def _complete_login(
    config: SeoConfig,
    store: SecretStore,
    *,
    client_id: str,
    client_secret: str,
    scopes: tuple[str, ...],
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange the code, persist the grant and build the token-free result payload."""
    response = exchange_code(
        TOKEN_ENDPOINT,
        client_id,
        client_secret,
        code,
        redirect_uri,
        code_verifier,
        next_action="lv24-seo auth login --provider gsc",
    )
    _persist(store, response, scopes)
    store.delete(CODE_VERIFIER_SECRET)
    expires_at = _expires_at(response)
    return {
        "provider": "gsc",
        "flow": "oauth",
        "scopes": list(scopes),
        "expires_at": expires_at,
        "target": _target(config),
        "next": "lv24-seo properties",
    }


def _persist(
    store: SecretStore,
    response: TokenResponse,
    requested_scopes: tuple[str, ...],
) -> None:
    store.set(ACCESS_TOKEN_SECRET, response.access_token)
    if response.refresh_token:
        store.set(REFRESH_TOKEN_SECRET, response.refresh_token)
    store.set(EXPIRES_AT_SECRET, _expires_at(response))
    # Google normally echoes the granted scope. When it does not, the request that just
    # succeeded is the best record of the grant; an empty value would turn the scope check
    # into a permanent "unknown".
    scopes = tuple(part for part in response.scope.split() if part) or requested_scopes
    if scopes:
        store.set(SCOPE_SECRET, " ".join(scopes))


def _expires_at(response: TokenResponse) -> str:
    expires_at = utc_now() + timedelta(seconds=response.expires_in)
    return expires_at.astimezone(UTC).isoformat(timespec="seconds")


def _manual_redirect_uri(client: OAuthClientConfig, port: int) -> str:
    """Rebuild the redirect URI used by the authorization request for a ``--code`` exchange.

    Google requires the token request to repeat the exact ``redirect_uri``. The loopback flow
    uses ``http://127.0.0.1:<port>/callback``; a headless setup can pin the value in
    ``[auth.gsc].redirect_uri`` instead.
    """
    configured = client.redirect_uri.strip()
    if configured:
        return configured
    if port <= 0:
        raise SeoError(
            code=INVALID_INPUT,
            message="cannot rebuild the OAuth redirect URI for --code",
            next_action=(
                "pass --port with the port used by the authorization URL, "
                "or set [auth.gsc].redirect_uri in seo.toml"
            ),
        )
    return f"http://127.0.0.1:{port}/callback"


def _target(config: SeoConfig) -> str:
    gsc = config.providers.gsc
    return gsc.property.strip() if gsc is not None else ""


def _stored(store: SecretStore, name: str) -> str:
    value = store.get(name)
    return value.strip() if value is not None else ""
