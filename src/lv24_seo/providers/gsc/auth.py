"""Google service-account authentication for the Search Console adapter.

Two credential forms are supported:

* a plain OAuth access token (the historical behaviour) forwarded as
  ``Authorization: Bearer <token>``;
* a Google service-account JSON key. The adapter signs a short-lived RS256 JWT,
  exchanges it at the key's ``token_uri`` for an access token, caches that token
  and refreshes it shortly before expiry.

Security rules
--------------
* The JSON document, the private key and the signed assertion never appear in an
  exception message, ``repr`` or log line. Only the file path is reported, and
  the path is configuration, not a secret.
* The key file must be absolute: a relative path would silently depend on the
  process working directory (the same class of bug that ``storage.data_dir``
  anchoring fixed).
* The parsed credential is cached by the token provider; the file is read once
  per process, not once per request.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lv24_seo.core.errors import AUTH_INVALID, CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient

__all__ = [
    "DEFAULT_TOKEN_URI",
    "JWT_BEARER_GRANT_TYPE",
    "SERVICE_ACCOUNT_TYPE",
    "WEBMASTERS_READONLY_SCOPE",
    "GscTokenProvider",
    "ServiceAccountCredentials",
    "ServiceAccountTokenProvider",
    "StaticTokenProvider",
    "load_service_account_file",
    "service_account_file_problem",
    "token_provider_from_token",
]

SERVICE_ACCOUNT_TYPE = "service_account"
WEBMASTERS_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint URL
JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
JWT_LIFETIME_SECONDS = 3600
# Refresh before the provider expires the token so a request never races the clock.
TOKEN_REFRESH_MARGIN_SECONDS = 60.0

SERVICE_ACCOUNT_FILE_NEXT_ACTION = (
    "download a fresh service-account JSON key from Google Cloud, store it outside the "
    "repository, and point providers.gsc.service_account_file or "
    "LV24_SEO_GSC_SERVICE_ACCOUNT_FILE at its absolute path"
)


class GscTokenProvider(Protocol):
    """Supply the bearer token for one GSC request; implementations may refresh."""

    def access_token(self) -> str: ...


class StaticTokenProvider:
    """A plain OAuth access token. The value never appears in ``repr``."""

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token.strip()

    def __repr__(self) -> str:
        return "StaticTokenProvider(token=***)"

    def access_token(self) -> str:
        return self._token


@dataclass(frozen=True, slots=True, repr=False)
class ServiceAccountCredentials:
    """The non-secret metadata and the private key of one service-account key."""

    client_email: str
    private_key: str
    private_key_id: str = ""
    token_uri: str = DEFAULT_TOKEN_URI

    def __repr__(self) -> str:
        return (
            "ServiceAccountCredentials("
            f"client_email={self.client_email!r}, "
            f"private_key_id={self.private_key_id!r}, "
            "private_key=***)"
        )


class ServiceAccountTokenProvider:
    """Sign a service-account JWT and cache the exchanged access token.

    ``clock`` is injectable so tests can advance time without sleeping. The lock
    makes a shared MCP server thread-safe: concurrent requests trigger at most
    one token exchange.
    """

    def __init__(
        self,
        credentials: ServiceAccountCredentials,
        *,
        http: HttpClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._http = http if http is not None else HttpClient()
        self._clock = clock
        self._token: str | None = None
        self._refresh_at = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            "ServiceAccountTokenProvider("
            f"client_email={self._credentials.client_email!r}, token=***)"
        )

    def access_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._refresh_at:
                return self._token
            token, expires_in = self._exchange(now)
            self._token = token
            # A token that expires inside the margin is refreshed on the next call.
            self._refresh_at = now + max(0.0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
            return token

    def _exchange(self, now: float) -> tuple[str, float]:
        assertion = _build_assertion(self._credentials, int(now))
        try:
            payload = self._http.post_json(
                self._credentials.token_uri,
                data={
                    "grant_type": JWT_BEARER_GRANT_TYPE,
                    "assertion": assertion,
                },
            )
        except SeoError as exc:
            raise _exchange_error(exc) from None

        if not isinstance(payload, Mapping):
            raise SeoError(
                code=AUTH_INVALID,
                message="Google returned an unexpected service-account token response",
                next_action=SERVICE_ACCOUNT_FILE_NEXT_ACTION,
                retryable=False,
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise SeoError(
                code=AUTH_INVALID,
                message="Google returned no access token for the service account",
                next_action=SERVICE_ACCOUNT_FILE_NEXT_ACTION,
                retryable=False,
            )
        return token.strip(), _expires_in(payload.get("expires_in"))


def token_provider_from_token(
    token: str, *, source: str = "LV24_SEO_GSC_TOKEN"
) -> GscTokenProvider:
    """Turn a stored ``gsc_token`` value into a provider.

    A plain string keeps the historical bearer-token behaviour. A JSON object is
    accepted only when it is a service-account key: a JSON-looking value that is
    not a key must fail loudly instead of being sent as a bearer token.
    """
    value = token.strip()
    if not value:
        raise SeoError(
            code=AUTH_INVALID,
            message=f"{source} is empty",
            next_action=SERVICE_ACCOUNT_FILE_NEXT_ACTION,
        )
    if not value.startswith("{"):
        return StaticTokenProvider(value)
    try:
        document: Any = json.loads(value)
    except json.JSONDecodeError:
        raise _config_error(f"{source} looks like JSON but is not valid JSON") from None
    if not isinstance(document, Mapping):
        raise _config_error(f"{source} must be a JSON object or a plain access token")
    return ServiceAccountTokenProvider(
        parse_service_account(cast(Mapping[str, Any], document), source=source)
    )


def service_account_file_problem(raw: str) -> str | None:
    """Return a safe, actionable problem string, or ``None`` when the path is usable.

    This is the cheap readiness check used by ``properties`` and ``doctor``: it
    stats the path and never reads the JSON, so it cannot leak the key.
    """
    text = raw.strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return f"Google service account file path must be absolute: {text}"
    try:
        exists = candidate.exists()
        is_file = candidate.is_file()
        readable = os.access(candidate, os.R_OK)
    except (OSError, ValueError):
        return f"Google service account file path is invalid: {text}"
    if not exists:
        return f"Google service account file not found: {candidate}"
    if not is_file:
        return f"Google service account path is not a file: {candidate}"
    if not readable:
        return f"Google service account file is not readable: {candidate}"
    return None


def load_service_account_file(raw: str) -> ServiceAccountCredentials:
    """Read, validate and cache-free parse a service-account JSON key.

    Callers build the client once and reuse it, so this runs once per process.
    """
    problem = service_account_file_problem(raw)
    if problem is not None:
        raise _config_error(problem)
    path = Path(raw.strip()).expanduser()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        raise _config_error(f"cannot read the Google service account file at {path}") from None
    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError:
        raise _config_error(
            f"the Google service account file at {path} is not valid JSON"
        ) from None
    if not isinstance(document, Mapping):
        raise _config_error(f"the Google service account file at {path} is not a JSON object")
    return parse_service_account(cast(Mapping[str, Any], document), source=str(path))


def parse_service_account(document: Mapping[str, Any], *, source: str) -> ServiceAccountCredentials:
    """Validate the service-account shape without ever echoing the key material."""
    if document.get("type") != SERVICE_ACCOUNT_TYPE:
        raise _config_error(
            f"{source} is not a service-account key (type must be {SERVICE_ACCOUNT_TYPE!r})"
        )
    email = document.get("client_email")
    private_key = document.get("private_key")
    if not isinstance(email, str) or not email.strip():
        raise _config_error(f"{source} has no client_email")
    if not isinstance(private_key, str) or "PRIVATE KEY" not in private_key:
        raise _config_error(f"{source} has no usable private_key")

    key_id = document.get("private_key_id")
    token_uri = document.get("token_uri")
    return ServiceAccountCredentials(
        client_email=email.strip(),
        private_key=private_key,
        private_key_id=key_id.strip() if isinstance(key_id, str) else "",
        token_uri=(
            token_uri.strip()
            if isinstance(token_uri, str) and token_uri.strip()
            else DEFAULT_TOKEN_URI
        ),
    )


def _build_assertion(credentials: ServiceAccountCredentials, issued_at: int) -> str:
    """Build the RS256 JWT-bearer assertion Google expects."""
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if credentials.private_key_id:
        header["kid"] = credentials.private_key_id
    claims: dict[str, Any] = {
        "iss": credentials.client_email,
        "scope": WEBMASTERS_READONLY_SCOPE,
        "aud": credentials.token_uri,
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
    }
    signing_input = f"{_b64url_json(header)}.{_b64url_json(claims)}"
    signature = _private_key(credentials.private_key).sign(
        signing_input.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input}.{_b64url(signature)}"


def _private_key(pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (TypeError, ValueError):
        raise _config_error("the Google service account private key could not be loaded") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise _config_error("the Google service account private key is not an RSA key")
    return key


def _b64url_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(raw)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _expires_in(raw: object) -> float:
    if isinstance(raw, bool):
        return float(JWT_LIFETIME_SECONDS)
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else float(JWT_LIFETIME_SECONDS)
    if isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return float(JWT_LIFETIME_SECONDS)
        return value if value > 0 else float(JWT_LIFETIME_SECONDS)
    return float(JWT_LIFETIME_SECONDS)


def _exchange_error(exc: SeoError) -> SeoError:
    if exc.code in {INVALID_INPUT, AUTH_INVALID}:
        return SeoError(
            code=AUTH_INVALID,
            message="Google rejected the service-account token request",
            next_action=(
                "verify that the key is active and the service account is added to the "
                "property as Full/Owner; create a new JSON key if it was revoked"
            ),
            retryable=False,
            detail={"status_code": exc.detail.get("status_code")},
        )
    return exc


def _config_error(message: str) -> SeoError:
    return SeoError(
        code=CONFIG_INVALID,
        message=message,
        next_action=SERVICE_ACCOUNT_FILE_NEXT_ACTION,
    )
