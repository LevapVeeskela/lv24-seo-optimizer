"""Yandex OAuth 2.0 connector for Webmaster and Metrika.

Yandex issues one OAuth token per application, not per API: a single app can request
``webmaster:hostinfo`` and ``metrika:read`` together, and the resulting access token works
against both APIs. The CLI keeps the two provider entries separate anyway, because the
Webmaster and Metrika engines are configured (and can be authorized) independently.

Flow
----
API-access apps use the ``verification_code`` redirect: after the user approves access,
Yandex shows a short code that the CLI exchanges for tokens. This avoids a loopback HTTP
server, which is what makes the flow usable on a VPS and over SSH.

Security rules
--------------
* Access and refresh tokens never appear in :class:`~lv24_seo.core.errors.SeoError`
  messages, ``detail``, ``repr`` or logs. Provider error bodies are reduced to a small
  machine code such as ``invalid_grant``.
* Token requests are form-encoded POSTs; the client secret is sent in the body, never in
  a URL or a header that could be logged.
* A refresh is attempted only when it can succeed: the client id and client secret must
  both be available. Otherwise the caller gets an actionable ``AUTH_INVALID`` instead of
  a request that would use an expired token.

Yandex token lifetimes
----------------------
Yandex can issue either a short-lived access token with a refresh token or a long-lived
access token without one. The connector treats a missing expiry as "long-lived" only when
no refresh token exists; when a refresh token is present, a token without a stored expiry
is refreshed because its freshness cannot be proven. This keeps static-token users working
while never silently using a token that is known to be expired.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now

__all__ = [
    "AUTHORIZE_URL",
    "DEFAULT_SCOPES",
    "REFRESH_MARGIN_SECONDS",
    "TOKEN_URL",
    "VERIFICATION_CODE_REDIRECT_URI",
    "OAuthTokens",
    "authorization_url",
    "ensure_fresh_access_token",
    "exchange_code",
    "login_next_action",
    "provider_scope_name",
    "provider_secret_names",
    "refresh_token",
    "store_tokens",
]

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"  # noqa: S105 - an endpoint URL
#: Yandex shows this page after approval; the user copies the code it displays.
VERIFICATION_CODE_REDIRECT_URI = "https://oauth.yandex.ru/verification_code"
DEFAULT_SCOPES: tuple[str, ...] = ("webmaster:hostinfo", "metrika:read")
#: Refresh before the provider expires the token so a request never races the clock.
REFRESH_MARGIN_SECONDS = 60.0
CLIENT_SECRET_NAME = "yandex_oauth_client_secret"  # noqa: S105 - a secret-store key
CLIENT_ID_ENV = "LV24_SEO_YANDEX_CLIENT_ID"
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: ``(access, refresh, expiry)`` secret names per provider entry.
_PROVIDER_KEYS: dict[str, tuple[str, str, str]] = {
    "yandex": ("yandex_webmaster_token", "yandex_refresh_token", "yandex_token_expires_at"),
    "metrika": (
        "yandex_metrika_token",
        "yandex_metrika_refresh_token",
        "yandex_metrika_token_expires_at",
    ),
}
_PROVIDER_ALIASES: dict[str, str] = {
    "yandex": "yandex",
    "webmaster": "yandex",
    "yandex_webmaster": "yandex",
    "metrika": "metrika",
    "yandex_metrika": "metrika",
}
#: Granted scopes, stored as one space-separated string (the token endpoint's shape).
_SCOPE_KEYS: dict[str, str] = {
    "yandex": "yandex_token_scopes",
    "metrika": "yandex_metrika_token_scopes",
}
_PROVIDER_LABELS: dict[str, str] = {
    "yandex": "Yandex Webmaster",
    "metrika": "Yandex Metrika",
}


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokens:
    """One Yandex token response, with the tokens masked in ``repr``."""

    access_token: str
    refresh_token: str = ""
    expires_in: int | None = None
    scope: tuple[str, ...] = ()
    expires_at: datetime | None = field(default=None)

    def __repr__(self) -> str:
        refresh = "***" if self.refresh_token else ""
        return (
            f"OAuthTokens(access_token=***, refresh_token={refresh!r}, "
            f"expires_in={self.expires_in!r}, scope={self.scope!r}, "
            f"expires_at={self.expires_at!r})"
        )


def authorization_url(
    client_id: str,
    scopes: Sequence[str],
    state: str,
    code_challenge: str | None = None,
    redirect_uri: str = VERIFICATION_CODE_REDIRECT_URI,
) -> str:
    """Build the Yandex authorization URL.

    ``state`` is required: it is the only CSRF protection the verification-code flow has.
    PKCE is optional because Yandex's ``verification_code`` redirect is copy/paste based and
    a verifier generated in one CLI invocation cannot be recovered by the next one.
    """
    cid = client_id.strip()
    if not cid:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="Yandex OAuth client_id is not configured",
            next_action=(
                "set [auth.yandex] client_id in seo.toml or LV24_SEO_YANDEX_CLIENT_ID, "
                "then rerun `lv24-seo auth login --provider yandex`"
            ),
        )
    state_value = state.strip()
    if not state_value:
        raise SeoError(
            code=INVALID_INPUT,
            message="OAuth state must not be empty",
            next_action="generate a random state value for every authorization request",
        )

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri.strip() or VERIFICATION_CODE_REDIRECT_URI,
        "state": state_value,
    }
    scope = " ".join(part.strip() for part in scopes if part.strip())
    if scope:
        params["scope"] = scope
    challenge = (code_challenge or "").strip()
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str = VERIFICATION_CODE_REDIRECT_URI,
    code_verifier: str = "",
    provider: str = "yandex",
    http: HttpClient | None = None,
    now: Callable[[], datetime] = utc_now,
) -> OAuthTokens:
    """Exchange a verification code for an access token and a refresh token."""
    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": _required(code, name="authorization code"),
        "client_id": _required(client_id, name="client_id"),
        "client_secret": _required(client_secret, name="client secret"),
        "redirect_uri": redirect_uri.strip() or VERIFICATION_CODE_REDIRECT_URI,
    }
    verifier = code_verifier.strip()
    if verifier:
        payload["code_verifier"] = verifier
    return _token_request(
        payload,
        http=http,
        now=now,
        operation="authorization code exchange",
        next_action=f"rerun `{login_next_action(provider)}` with a fresh code",
    )


def refresh_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    provider: str = "yandex",
    http: HttpClient | None = None,
    now: Callable[[], datetime] = utc_now,
) -> OAuthTokens:
    """Exchange a refresh token for a new access token.

    Yandex may omit ``refresh_token`` in the response; in that case the caller keeps the
    existing one (see :func:`ensure_fresh_access_token`).
    """
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": _required(refresh_token, name="refresh token"),
        "client_id": _required(client_id, name="client_id"),
        "client_secret": _required(client_secret, name="client secret"),
    }
    return _token_request(
        payload,
        http=http,
        now=now,
        operation="token refresh",
        next_action=f"run `{login_next_action(provider)}` to authorize again",
    )


def provider_secret_names(provider: str) -> tuple[str, str, str]:
    """Return ``(access, refresh, expiry)`` secret names for ``yandex`` or ``metrika``."""
    key = _provider_key(provider)
    return _PROVIDER_KEYS[key]


def provider_scope_name(provider: str) -> str:
    """Return the secret name that stores the granted scopes for one provider entry."""
    return _SCOPE_KEYS[_provider_key(provider)]


def login_next_action(provider: str) -> str:
    """The exact command that re-authorizes one provider entry."""
    return f"lv24-seo auth login --provider {_provider_key(provider)}"


def store_tokens(
    store: SecretStore,
    provider: str,
    tokens: OAuthTokens,
    *,
    keep_refresh: bool = True,
    requested_scopes: Sequence[str] = (),
) -> None:
    """Persist a token response.

    ``keep_refresh`` is true for a refresh response, where Yandex may omit the refresh
    token and the existing one must survive. A fresh login passes ``False``: if the new
    grant has no refresh token, the previous grant's refresh token is stale and is removed.

    Scopes are stored as one space-separated string. A response that omits ``scope`` keeps
    the recorded list on a refresh; a fresh login records ``requested_scopes`` instead,
    because the exchange that just succeeded proves which scopes were granted.

    Environment-provided secrets are read-only. A refresh still returns a usable token for
    this process, so a ``WRITE_DISABLED`` from one of the companion keys is ignored; every
    other storage error propagates.
    """
    access_name, refresh_name, expiry_name = provider_secret_names(provider)
    _set_secret(store, access_name, tokens.access_token)
    if tokens.refresh_token:
        _set_secret(store, refresh_name, tokens.refresh_token)
    elif not keep_refresh:
        _delete_secret(store, refresh_name)
    if tokens.expires_at is None:
        _delete_secret(store, expiry_name)
    else:
        _set_secret(store, expiry_name, tokens.expires_at.isoformat(timespec="seconds"))
    _store_scopes(
        store,
        provider,
        tokens.scope or tuple(scope.strip() for scope in requested_scopes if scope.strip()),
        keep_existing=keep_refresh,
    )


def ensure_fresh_access_token(
    store: SecretStore,
    provider: str,
    *,
    client_id: str = "",
    http: HttpClient | None = None,
    now: Callable[[], datetime] = utc_now,
) -> str:
    """Return a usable access token, refreshing it when it is known to be expired.

    * a valid token (or a long-lived token without a refresh token) is returned unchanged;
    * a missing/expired token with a refresh token is refreshed and persisted;
    * an expired token without a refresh token is an ``AUTH_INVALID`` error rather than a
      request that the provider is guaranteed to reject;
    * a refresh failure is reported as ``AUTH_INVALID`` with the provider-specific login
      command, never as a request with the stale token.
    """
    key = _provider_key(provider)
    access_name, refresh_name, expiry_name = _PROVIDER_KEYS[key]
    access = _clean(store.get(access_name))
    refresh = _clean(store.get(refresh_name))
    expires_at = _parse_expiry(store.get(expiry_name))
    current = _now(now)

    if access is not None:
        if expires_at is None:
            if refresh is None:
                # A long-lived token without a refresh token: the historical static-token case.
                return access
        elif expires_at > current and (refresh is None or _is_fresh(expires_at, current)):
            # Still valid. Inside the refresh margin the token is refreshed only when a
            # refresh token exists; a static token keeps working until it actually expires.
            return access

    if refresh is None:
        if access is None:
            raise SeoError(
                code=AUTH_REQUIRED,
                message=f"{_PROVIDER_LABELS[key]} token is missing",
                next_action=login_next_action(key),
            )
        raise SeoError(
            code=AUTH_INVALID,
            message=(
                f"{_PROVIDER_LABELS[key]} access token expired and no refresh token is stored"
            ),
            next_action=login_next_action(key),
        )

    resolved_client_id = client_id.strip() or os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = _clean(store.get(CLIENT_SECRET_NAME))
    if not resolved_client_id or client_secret is None:
        raise SeoError(
            code=AUTH_INVALID,
            message=(
                f"{_PROVIDER_LABELS[key]} access token needs a refresh, but the OAuth client "
                "id or client secret is missing"
            ),
            next_action=(
                "store the client secret with `lv24-seo auth set --provider "
                "yandex_oauth_secret --stdin` and set [auth.yandex] client_id, then run "
                f"`{login_next_action(key)}`"
            ),
        )

    try:
        tokens = refresh_token(
            client_id=resolved_client_id,
            client_secret=client_secret,
            refresh_token=refresh,
            provider=key,
            http=http,
            now=now,
        )
    except SeoError as exc:
        raise SeoError(
            code=AUTH_INVALID,
            message=f"{_PROVIDER_LABELS[key]} OAuth refresh failed; authorize again",
            next_action=login_next_action(key),
            detail={"status_code": exc.detail.get("status_code")},
        ) from None

    store_tokens(store, key, tokens)
    return tokens.access_token


def _token_request(
    payload: Mapping[str, str],
    *,
    http: HttpClient | None,
    now: Callable[[], datetime],
    operation: str,
    next_action: str,
) -> OAuthTokens:
    """POST a form-encoded token request and map every failure to a safe ``SeoError``."""
    client = http if http is not None else HttpClient()
    try:
        response = client.request(
            "POST",
            TOKEN_URL,
            data=dict(payload),
            raise_for_status=False,
        )
    except SeoError as exc:
        # The shared client already redacts the URL and never echoes headers or bodies.
        if exc.code == UPSTREAM_UNAVAILABLE:
            raise
        raise SeoError(
            code=AUTH_INVALID,
            message=f"Yandex OAuth {operation} request failed",
            next_action=next_action,
            detail={"status_code": exc.detail.get("status_code")},
        ) from None

    try:
        status_code = response.status_code
        try:
            body: object = response.json()
        except ValueError:
            body = None
    finally:
        response.close()

    if status_code >= 400:
        raise _response_error(
            status_code,
            body,
            operation=operation,
            next_action=next_action,
        )
    if not isinstance(body, Mapping):
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"Yandex returned an unexpected OAuth {operation} response",
            retryable=False,
            detail={"status_code": status_code},
        )
    return _parse_tokens(body, now)


def _response_error(
    status_code: int,
    body: object,
    *,
    operation: str,
    next_action: str,
) -> SeoError:
    provider_code = _safe_error_code(body)
    detail: dict[str, Any] = {"status_code": status_code}
    if provider_code:
        detail["error"] = provider_code
    if status_code == 429 or status_code >= 500:
        return SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"Yandex OAuth {operation} is temporarily unavailable (HTTP {status_code})",
            retryable=True,
            detail=detail,
        )
    if 400 <= status_code < 500:
        return SeoError(
            code=AUTH_INVALID,
            message=f"Yandex rejected the OAuth {operation} request (HTTP {status_code})",
            next_action=next_action,
            retryable=False,
            detail=detail,
        )
    return SeoError(
        code=UPSTREAM_UNAVAILABLE,
        message=f"Yandex OAuth {operation} returned HTTP {status_code}",
        retryable=False,
        detail=detail,
    )


def _parse_tokens(payload: Mapping[str, Any], now: Callable[[], datetime]) -> OAuthTokens:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access.strip():
        raise SeoError(
            code=AUTH_INVALID,
            message="Yandex returned no access token",
            next_action="rerun `lv24-seo auth login --provider yandex`",
            retryable=False,
        )
    refresh = payload.get("refresh_token")
    expires_in = _positive_int(payload.get("expires_in"))
    scope = _parse_scope(payload.get("scope"))
    expires_at = _now(now) + timedelta(seconds=expires_in) if expires_in is not None else None
    return OAuthTokens(
        access_token=access.strip(),
        refresh_token=refresh.strip() if isinstance(refresh, str) else "",
        expires_in=expires_in,
        scope=scope,
        expires_at=expires_at,
    )


def _provider_key(provider: str) -> str:
    key = _PROVIDER_ALIASES.get(provider.strip().lower())
    if key is None:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown Yandex OAuth provider {provider!r}",
            next_action="use `yandex` or `metrika`",
        )
    return key


def _required(value: str, *, name: str) -> str:
    text = value.strip()
    if not text:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"Yandex OAuth {name} is empty",
            next_action="provide every OAuth field before requesting a token",
        )
    return text


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _is_fresh(expires_at: datetime, current: datetime) -> bool:
    return expires_at > current + timedelta(seconds=REFRESH_MARGIN_SECONDS)


def _parse_expiry(raw: str | None) -> datetime | None:
    """Parse a stored ISO-8601 expiry; an unreadable value means "unknown"."""
    text = _clean(raw)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _positive_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return int(value)


def _parse_scope(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    if isinstance(raw, (list, tuple)):
        return tuple(part.strip() for part in raw if isinstance(part, str) and part.strip())
    return ()


def _safe_error_code(body: object) -> str:
    """Return a provider machine code such as ``invalid_grant``; never a description."""
    if not isinstance(body, Mapping):
        return ""
    raw = body.get("error")
    if isinstance(raw, str) and _ERROR_CODE_RE.fullmatch(raw):
        return raw
    return ""


def _set_secret(store: SecretStore, name: str, value: str) -> None:
    try:
        store.set(name, value)
    except SeoError as exc:
        if exc.code != WRITE_DISABLED:
            raise


def _store_scopes(
    store: SecretStore,
    provider: str,
    scopes: Sequence[str],
    *,
    keep_existing: bool,
) -> None:
    """Write the space-separated scope list, or keep/delete the previous one.

    A refresh response may omit ``scope``; the previous list is still the best record.
    A fresh login without any scope information deletes the stale list instead of
    pretending the new grant covers it.
    """
    name = provider_scope_name(provider)
    cleaned = tuple(scope.strip() for scope in scopes if scope.strip())
    if cleaned:
        _set_secret(store, name, " ".join(cleaned))
    elif not keep_existing:
        _delete_secret(store, name)


def _delete_secret(store: SecretStore, name: str) -> None:
    try:
        store.delete(name)
    except SeoError as exc:
        if exc.code != WRITE_DISABLED:
            raise
