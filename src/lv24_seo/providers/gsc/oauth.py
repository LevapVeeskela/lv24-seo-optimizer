"""Google Search Console OAuth 2.0 connector: authorization URL and refreshable tokens.

The installed-app flow (RFC 8252) is used: a Desktop OAuth client from Google Cloud, a
loopback redirect on ``127.0.0.1``, PKCE and ``access_type=offline`` so Google issues a
refresh token. The client id is public configuration; the client secret and every token
live in :class:`~lv24_seo.core.secrets.SecretStore`.

Secret names
------------
``gsc_access_token``          short-lived bearer token, refreshed by the provider
``gsc_refresh_token``         long-lived token from the first consent
``gsc_token_expires_at``      ISO-8601 UTC expiry of the access token
``gsc_oauth_client_secret``   the Desktop client secret
``gsc_oauth_code_verifier``   short-lived PKCE verifier kept between ``--no-browser`` and
                              ``--code`` (deleted once the code is exchanged)

A service account remains a supported alternative; :class:`OAuthTokenProvider` is only
constructed when a refresh token is stored.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx

from lv24_seo.core.errors import AUTH_REQUIRED, SeoError
from lv24_seo.core.oauth import refresh_access_token
from lv24_seo.core.secrets import SecretStore

__all__ = [
    "ACCESS_TOKEN_SECRET",
    "AUTHORIZATION_ENDPOINT",
    "CLIENT_SECRET_NAME",
    "CODE_VERIFIER_SECRET",
    "EXPIRES_AT_SECRET",
    "LOGIN_NEXT_ACTION",
    "OAUTH_SOURCE",
    "REFRESH_MARGIN_SECONDS",
    "REFRESH_TOKEN_SECRET",
    "TOKEN_ENDPOINT",
    "OAuthTokenProvider",
    "authorization_url",
]

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint URL
ACCESS_TOKEN_SECRET = "gsc_access_token"  # noqa: S105 - a secret name, not a value
REFRESH_TOKEN_SECRET = "gsc_refresh_token"  # noqa: S105 - a secret name, not a value
EXPIRES_AT_SECRET = "gsc_token_expires_at"  # noqa: S105 - a secret name, not a value
CLIENT_SECRET_NAME = "gsc_oauth_client_secret"  # noqa: S105 - a secret name, not a value
CODE_VERIFIER_SECRET = "gsc_oauth_code_verifier"  # noqa: S105 - a secret name, not a value
OAUTH_SOURCE = "oauth_refresh_token"
LOGIN_NEXT_ACTION = "lv24-seo auth login --provider gsc"

# Refresh this long before Google expires the token so a request never races the clock.
REFRESH_MARGIN_SECONDS = 60.0


def authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    code_challenge: str,
) -> str:
    """Build the Google consent URL for the installed-app code flow.

    ``access_type=offline`` asks for a refresh token and ``prompt=consent`` forces the
    consent screen on every login, because Google returns a refresh token only then.
    """
    scope = " ".join(item.strip() for item in scopes if item.strip())
    query = urlencode(
        [
            ("response_type", "code"),
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("scope", scope),
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("state", state),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
        ],
        quote_via=quote,
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


class OAuthTokenProvider:
    """Supply GSC bearer tokens, refreshing the stored OAuth grant shortly before expiry.

    The provider reads the secret store on every call instead of caching a token in memory:
    the MCP server and the CLI share one OS keychain entry, so a login performed by the CLI
    must become visible to an already-running server without a restart. A lock keeps
    concurrent requests from starting two refreshes.
    """

    def __init__(
        self,
        *,
        store: SecretStore,
        client_id: str,
        client_secret: str,
        token_url: str = TOKEN_ENDPOINT,
        clock: Callable[[], float] = time.time,
        transport: httpx.BaseTransport | None = None,
        refresh_margin: float = REFRESH_MARGIN_SECONDS,
    ) -> None:
        self._store = store
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._token_url = token_url
        self._clock = clock
        self._transport = transport
        self._refresh_margin = refresh_margin
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"OAuthTokenProvider(client_id={self._client_id!r}, token=***)"

    def access_token(self) -> str:
        """Return a valid access token, refreshing it when it is missing or near expiry."""
        with self._lock:
            token = _stored(self._store, ACCESS_TOKEN_SECRET)
            expires_at = self._expires_at()
            now = self._clock()
            if token and expires_at is not None and now + self._refresh_margin < expires_at:
                return token
            return self._refresh(now)

    def _expires_at(self) -> float | None:
        raw = _stored(self._store, EXPIRES_AT_SECRET)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()

    def _refresh(self, now: float) -> str:
        refresh_token = _stored(self._store, REFRESH_TOKEN_SECRET)
        if not refresh_token:
            raise SeoError(
                code=AUTH_REQUIRED,
                message="Google Search Console refresh token is missing",
                next_action=LOGIN_NEXT_ACTION,
            )
        response = refresh_access_token(
            self._token_url,
            self._client_id,
            self._client_secret,
            refresh_token,
            transport=self._transport,
            next_action=LOGIN_NEXT_ACTION,
        )
        self._store.set(ACCESS_TOKEN_SECRET, response.access_token)
        if response.refresh_token:
            # Google usually omits the refresh token on refresh; keep the existing grant.
            self._store.set(REFRESH_TOKEN_SECRET, response.refresh_token)
        expires_at = datetime.fromtimestamp(now + response.expires_in, tz=UTC)
        self._store.set(EXPIRES_AT_SECRET, expires_at.isoformat(timespec="seconds"))
        return response.access_token


def _stored(store: SecretStore, name: str) -> str:
    value = store.get(name)
    return value.strip() if value is not None else ""
