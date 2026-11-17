"""OAuth readiness diagnostics shared by ``auth status`` and ``doctor``.

The connectors can fail for very different reasons: no registered app, no client secret,
no token, an expired token, a token granted with fewer scopes than the config asks for, or
a redirect URI the provider will reject. Reporting all of them as "auth required" made the
first live setup impossible to debug, so every provider is classified into one stable code:

``oauth_client_missing``
    ``[auth.gsc].client_id`` / ``[auth.yandex].client_id`` is empty (or a documented
    placeholder). Register an app and set the client id.
``oauth_client_secret_missing``
    The client id is set but ``gsc_oauth_client_secret`` / ``yandex_oauth_client_secret``
    is not stored.
``oauth_token_missing``
    Client id and secret are present but neither an access nor a refresh token is stored.
``oauth_token_expired``
    The stored expiry is in the past. With a refresh token the next request refreshes it;
    without one the user must log in again.
``oauth_scope_missing``
    The recorded token scopes do not include the configured scopes.
``oauth_scope_unknown``
    No scope was ever recorded (a grant stored before scopes were persisted). This is a
    limitation, never a pass.
``oauth_redirect_invalid``
    A configured redirect URI would be rejected: Google requires
    ``http://127.0.0.1:<port>/...``; Yandex requires
    ``https://oauth.yandex.ru/verification_code``. An empty value means the provider
    default and is valid.
``oauth_ready``
    Client id, client secret, token, scopes and redirect all check out.

Precedence is the order above, with the redirect check placed after the client secret and
before the token checks: a redirect that cannot complete a login must be fixed before the
user is told to log in.

Scope storage
-------------
The granted scopes are stored as **one space-separated string**, the same shape the token
endpoints return, under ``gsc_token_scopes``, ``yandex_token_scopes`` and
``yandex_metrika_token_scopes``. :func:`parse_recorded_scopes` also accepts a JSON array so
a hand-edited secret file keeps working. Logout deletes the entry.

No value from the secret store is ever placed in ``detail``, ``next_action``, a
``limitations`` entry or a ``repr``; only names, booleans and the non-secret expiry/scope
metadata are reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from lv24_seo.core.config import OAuthClientConfig, SeoConfig
from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.oauth import is_placeholder_client_id
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.gsc.auth import WEBMASTERS_READONLY_SCOPE
from lv24_seo.providers.gsc.oauth import ACCESS_TOKEN_SECRET as GSC_ACCESS_TOKEN_SECRET
from lv24_seo.providers.gsc.oauth import CLIENT_SECRET_NAME as GSC_CLIENT_SECRET_NAME
from lv24_seo.providers.gsc.oauth import EXPIRES_AT_SECRET as GSC_EXPIRES_AT_SECRET
from lv24_seo.providers.gsc.oauth import REFRESH_TOKEN_SECRET as GSC_REFRESH_TOKEN_SECRET
from lv24_seo.providers.gsc.oauth import SCOPE_SECRET as GSC_SCOPE_SECRET
from lv24_seo.providers.yandex_oauth import CLIENT_SECRET_NAME as YANDEX_CLIENT_SECRET_NAME
from lv24_seo.providers.yandex_oauth import DEFAULT_SCOPES as YANDEX_DEFAULT_SCOPES
from lv24_seo.providers.yandex_oauth import (
    VERIFICATION_CODE_REDIRECT_URI,
    provider_scope_name,
    provider_secret_names,
)

__all__ = [
    "OAUTH_CLIENT_MISSING",
    "OAUTH_CLIENT_SECRET_MISSING",
    "OAUTH_PROVIDERS",
    "OAUTH_READY",
    "OAUTH_REDIRECT_INVALID",
    "OAUTH_SCOPE_MISSING",
    "OAUTH_SCOPE_UNKNOWN",
    "OAUTH_STATES",
    "OAUTH_TOKEN_EXPIRED",
    "OAUTH_TOKEN_MISSING",
    "OAuthReadiness",
    "oauth_readiness",
    "oauth_readiness_all",
    "parse_recorded_scopes",
]

OAUTH_CLIENT_MISSING = "oauth_client_missing"
OAUTH_CLIENT_SECRET_MISSING = "oauth_client_secret_missing"  # noqa: S105 - a state code
OAUTH_TOKEN_MISSING = "oauth_token_missing"  # noqa: S105 - a state code
OAUTH_TOKEN_EXPIRED = "oauth_token_expired"  # noqa: S105 - a state code
OAUTH_SCOPE_MISSING = "oauth_scope_missing"
OAUTH_SCOPE_UNKNOWN = "oauth_scope_unknown"
OAUTH_REDIRECT_INVALID = "oauth_redirect_invalid"
OAUTH_READY = "oauth_ready"

#: Stable provider keys, in report order. ``metrika`` shares the Yandex app but has its own
#: token slot, so it gets its own state.
OAUTH_PROVIDERS: tuple[str, ...] = ("gsc", "yandex", "metrika")

OAUTH_STATES: tuple[str, ...] = (
    OAUTH_CLIENT_MISSING,
    OAUTH_CLIENT_SECRET_MISSING,
    OAUTH_TOKEN_MISSING,
    OAUTH_TOKEN_EXPIRED,
    OAUTH_SCOPE_MISSING,
    OAUTH_SCOPE_UNKNOWN,
    OAUTH_REDIRECT_INVALID,
    OAUTH_READY,
)

_REDIRECT_KIND = Literal["google", "yandex"]
_GOOGLE_REGISTRATION_URL = "https://console.cloud.google.com/apis/credentials"
_YANDEX_REGISTRATION_URL = "https://oauth.yandex.ru/"
_GSC_CLIENT_ID_ENV = "LV24_SEO_GSC_CLIENT_ID"
_YANDEX_CLIENT_ID_ENV = "LV24_SEO_YANDEX_CLIENT_ID"


@dataclass(frozen=True, slots=True)
class _ProviderSpec:
    """Where one provider keeps its OAuth configuration and secrets."""

    key: str
    label: str
    section: str
    client_id_env: str
    client_secret: str
    access_token: str
    refresh_token: str
    expires_at: str
    scopes: str
    default_scopes: tuple[str, ...]
    redirect_kind: _REDIRECT_KIND
    registration_url: str
    login_command: str
    secret_provider: str


def _build_specs() -> dict[str, _ProviderSpec]:
    yandex_access, yandex_refresh, yandex_expiry = provider_secret_names("yandex")
    metrika_access, metrika_refresh, metrika_expiry = provider_secret_names("metrika")
    return {
        "gsc": _ProviderSpec(
            key="gsc",
            label="Google Search Console",
            section="[auth.gsc]",
            client_id_env=_GSC_CLIENT_ID_ENV,
            client_secret=GSC_CLIENT_SECRET_NAME,
            access_token=GSC_ACCESS_TOKEN_SECRET,
            refresh_token=GSC_REFRESH_TOKEN_SECRET,
            expires_at=GSC_EXPIRES_AT_SECRET,
            scopes=GSC_SCOPE_SECRET,
            default_scopes=(WEBMASTERS_READONLY_SCOPE,),
            redirect_kind="google",
            registration_url=_GOOGLE_REGISTRATION_URL,
            login_command="lv24-seo auth login --provider gsc",
            secret_provider="gsc_oauth_secret",  # noqa: S106 - a provider name, not a value
        ),
        "yandex": _ProviderSpec(
            key="yandex",
            label="Yandex Webmaster",
            section="[auth.yandex]",
            client_id_env=_YANDEX_CLIENT_ID_ENV,
            client_secret=YANDEX_CLIENT_SECRET_NAME,
            access_token=yandex_access,
            refresh_token=yandex_refresh,
            expires_at=yandex_expiry,
            scopes=provider_scope_name("yandex"),
            default_scopes=YANDEX_DEFAULT_SCOPES,
            redirect_kind="yandex",
            registration_url=_YANDEX_REGISTRATION_URL,
            login_command="lv24-seo auth login --provider yandex",
            secret_provider="yandex_oauth_secret",  # noqa: S106 - a provider name, not a value
        ),
        "metrika": _ProviderSpec(
            key="metrika",
            label="Yandex Metrika",
            section="[auth.yandex]",
            client_id_env=_YANDEX_CLIENT_ID_ENV,
            client_secret=YANDEX_CLIENT_SECRET_NAME,
            access_token=metrika_access,
            refresh_token=metrika_refresh,
            expires_at=metrika_expiry,
            scopes=provider_scope_name("metrika"),
            default_scopes=YANDEX_DEFAULT_SCOPES,
            redirect_kind="yandex",
            registration_url=_YANDEX_REGISTRATION_URL,
            login_command="lv24-seo auth login --provider metrika",
            secret_provider="yandex_oauth_secret",  # noqa: S106 - a provider name, not a value
        ),
    }


_PROVIDER_SPECS: dict[str, _ProviderSpec] = _build_specs()


@dataclass(frozen=True, slots=True)
class OAuthReadiness:
    """The classified OAuth state of one provider, safe to print and serialise."""

    provider: str
    state: str
    detail: str
    client_id_present: bool
    client_secret_present: bool
    access_token_present: bool
    refresh_token_present: bool
    expires_at: str | None
    scopes_recorded: bool
    next_action: str
    limitations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True only for :data:`OAUTH_READY`; unknown scopes are not a pass."""
        return self.state == OAUTH_READY

    def as_dict(self) -> dict[str, Any]:
        """The ``auth status --json`` shape; tokens are represented by booleans only."""
        return {
            "provider": self.provider,
            "state": self.state,
            "detail": self.detail,
            "client_id_present": self.client_id_present,
            "client_secret_present": self.client_secret_present,
            "access_token_present": self.access_token_present,
            "refresh_token_present": self.refresh_token_present,
            "expires_at": self.expires_at,
            "scopes_recorded": self.scopes_recorded,
            "next_action": self.next_action,
            "limitations": list(self.limitations),
        }


def oauth_readiness_all(
    config: SeoConfig,
    store: SecretStore,
    *,
    now: datetime | None = None,
) -> dict[str, OAuthReadiness]:
    """Classify every OAuth provider in :data:`OAUTH_PROVIDERS`."""
    return {key: oauth_readiness(config, store, key, now=now) for key in OAUTH_PROVIDERS}


def oauth_readiness(
    config: SeoConfig,
    store: SecretStore,
    provider: str,
    *,
    now: datetime | None = None,
) -> OAuthReadiness:
    """Return the readiness state for ``gsc``, ``yandex`` or ``metrika``."""
    spec = _spec(provider)
    client = _client_config(config, spec.key)
    client_id = client.client_id.strip()
    client_secret = _secret(store, spec.client_secret)
    access = _secret(store, spec.access_token)
    refresh = _secret(store, spec.refresh_token)
    expiry_raw = _secret(store, spec.expires_at)
    expires_at, expiry_invalid = _parse_expiry(expiry_raw)
    recorded = parse_recorded_scopes(_secret(store, spec.scopes))
    configured = _configured_scopes(config, spec)
    current = _now(now)
    limitations: list[str] = []

    def build(state: str, detail: str, next_action: str) -> OAuthReadiness:
        return OAuthReadiness(
            provider=spec.key,
            state=state,
            detail=detail,
            client_id_present=bool(client_id),
            client_secret_present=bool(client_secret),
            access_token_present=bool(access),
            refresh_token_present=bool(refresh),
            expires_at=expiry_raw or None,
            scopes_recorded=bool(recorded),
            next_action=next_action,
            limitations=tuple(limitations),
        )

    if expiry_invalid:
        limitations.append("the stored token expiry is not valid ISO-8601; treated as unknown")
    if spec.key == "gsc" and _gsc_service_account_configured(config):
        limitations.append("GSC also has a service-account file configured; OAuth is optional")

    if not client_id:
        if _static_token_present(store, spec):
            limitations.append("a manually stored token may still work without OAuth")
        return build(
            OAUTH_CLIENT_MISSING,
            (
                f"OAuth client_id is not configured; register an app at "
                f"{spec.registration_url} and set {spec.section} client_id "
                f"(or {spec.client_id_env})"
            ),
            (
                f"create an OAuth app at {spec.registration_url}, then set "
                f"{spec.section} client_id or {spec.client_id_env}"
            ),
        )
    if is_placeholder_client_id(client_id):
        return build(
            OAUTH_CLIENT_MISSING,
            (
                f"OAuth client_id is a documented placeholder; register a real app at "
                f"{spec.registration_url}"
            ),
            f"replace the placeholder {spec.section} client_id with the app's real client id",
        )
    if not client_secret:
        return build(
            OAUTH_CLIENT_SECRET_MISSING,
            (
                f"OAuth client_id is set but {spec.client_secret} is not stored; run "
                f"`lv24-seo auth set --provider {spec.secret_provider} --stdin`"
            ),
            f"lv24-seo auth set --provider {spec.secret_provider} --stdin",
        )

    redirect_problem = _redirect_problem(spec, client.redirect_uri.strip())
    if redirect_problem:
        return build(
            OAUTH_REDIRECT_INVALID,
            redirect_problem,
            (
                f"clear {spec.section} redirect_uri to use the provider default, or set a "
                f"valid value"
            ),
        )

    if not access and not refresh:
        return build(
            OAUTH_TOKEN_MISSING,
            f"no OAuth access or refresh token is stored; run `{spec.login_command}`",
            spec.login_command,
        )

    if expires_at is not None and expires_at <= current:
        stamp = expiry_raw or expires_at.isoformat(timespec="seconds")
        if refresh:
            return build(
                OAUTH_TOKEN_EXPIRED,
                f"access token expired at {stamp}; refresh token present (refresh available)",
                f"run a read command (the token refreshes automatically) or `{spec.login_command}`",
            )
        limitations.append(
            "an expired token without a refresh token cannot be renewed automatically"
        )
        return build(
            OAUTH_TOKEN_EXPIRED,
            f"access token expired at {stamp} and no refresh token is stored; run "
            f"`{spec.login_command}`",
            spec.login_command,
        )

    if not recorded:
        limitations.append(
            "token scopes were never recorded, so scope coverage is unknown, not passed"
        )
        return build(
            OAUTH_SCOPE_UNKNOWN,
            "token scopes were never recorded; run a login or a refresh once to record them",
            f"run `{spec.login_command}` or a read command (a refresh records the granted scopes)",
        )

    missing = [scope for scope in configured if scope not in recorded]
    if missing:
        return build(
            OAUTH_SCOPE_MISSING,
            (
                f"recorded token scopes ({' '.join(recorded)}) do not include the configured "
                f"scopes ({' '.join(missing)}); run `{spec.login_command}`"
            ),
            (
                f"run `{spec.login_command}` and grant {', '.join(missing)}, or remove them "
                f"from {spec.section} scopes"
            ),
        )

    return build(
        OAUTH_READY,
        f"{spec.label} OAuth client, secret, token, scopes and redirect are ready",
        "",
    )


def parse_recorded_scopes(raw: str | None) -> tuple[str, ...]:
    """Parse a stored scope value.

    The writer stores one space-separated string; a JSON array is also accepted so a
    hand-edited secret file is not silently reported as "unknown".
    """
    text = (raw or "").strip()
    if not text:
        return ()
    if text.startswith("["):
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(item.strip() for item in parsed if isinstance(item, str) and item.strip())
    return tuple(part for part in text.split() if part)


def _spec(provider: str) -> _ProviderSpec:
    spec = _PROVIDER_SPECS.get(provider.strip().lower())
    if spec is None:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown OAuth provider {provider!r}",
            next_action=f"use one of: {', '.join(OAUTH_PROVIDERS)}",
        )
    return spec


def _client_config(config: SeoConfig, provider: str) -> OAuthClientConfig:
    return config.auth.gsc if provider == "gsc" else config.auth.yandex


def _configured_scopes(config: SeoConfig, spec: _ProviderSpec) -> tuple[str, ...]:
    scopes = tuple(
        scope.strip() for scope in _client_config(config, spec.key).scopes if scope.strip()
    )
    return scopes or spec.default_scopes


def _secret(store: SecretStore, name: str) -> str:
    value = store.get(name)
    return value.strip() if isinstance(value, str) else ""


def _now(now: datetime | None) -> datetime:
    value = now if now is not None else utc_now()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _parse_expiry(raw: str) -> tuple[datetime | None, bool]:
    text = raw.strip()
    if not text:
        return None, False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, True
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed), False


def _redirect_problem(spec: _ProviderSpec, value: str) -> str:
    """Return a safe problem string, or ``""`` when the redirect is valid."""
    if not value:
        return ""
    if spec.redirect_kind == "yandex":
        if value == VERIFICATION_CODE_REDIRECT_URI:
            return ""
        return (
            f"redirect_uri {value!r} is not {VERIFICATION_CODE_REDIRECT_URI}; clear it to "
            f"use the default"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return f"redirect_uri {value!r} is not a valid URL"
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or port < 1
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        return (
            f"redirect_uri {value!r} is not a loopback http://127.0.0.1:<port>/... URI; "
            f"clear it to use the default"
        )
    return ""


def _static_token_present(store: SecretStore, spec: _ProviderSpec) -> bool:
    """A manual token can keep reads working while OAuth itself is unconfigured."""
    if spec.key == "gsc":
        return bool(_secret(store, "gsc_token"))
    return bool(_secret(store, spec.access_token))


def _gsc_service_account_configured(config: SeoConfig) -> bool:
    gsc = config.providers.gsc
    return bool(gsc is not None and gsc.service_account_file.strip())
