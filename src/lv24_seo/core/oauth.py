"""Provider-agnostic OAuth 2.0 helpers: PKCE, the loopback callback and token requests.

The connector modules (``providers/*/oauth.py``) own the provider-specific URL shape and
secret names; this module owns the parts that must behave identically everywhere:

* **PKCE** (RFC 7636, S256). A verifier is 43-128 characters from the unreserved set;
  ``secrets.token_urlsafe`` already produces exactly that alphabet.
* **Loopback callback** (RFC 8252). The server binds ``127.0.0.1`` only -- never
  ``0.0.0.0`` -- so a code cannot be collected from another interface. The callback is
  single-use, the ``state`` is verified before the code is accepted, and the request path
  (which carries the code) is never logged.
* **Token exchange and refresh**. Errors map to the shared :class:`SeoError` codes so the
  CLI/MCP surfaces behave the same for every provider. A token value is never placed in an
  exception message, a ``detail`` mapping or a ``repr``.

No refresh scheduling lives here: a provider token provider decides *when* to refresh.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx

from lv24_seo.core.errors import AUTH_INVALID, AUTH_REQUIRED, UPSTREAM_UNAVAILABLE, SeoError

__all__ = [
    "CALLBACK_PATH",
    "DEFAULT_TIMEOUT_SECONDS",
    "LOOPBACK_HOST",
    "LoopbackServer",
    "TokenResponse",
    "exchange_code",
    "generate_pkce",
    "generate_state",
    "refresh_access_token",
]

LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_TOKEN_TIMEOUT_SECONDS = 30.0

# A verifier must be 43-128 characters; 64 random bytes encode to 86 URL-safe characters.
_PKCE_VERIFIER_BYTES = 64
_STATE_BYTES = 32


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE method."""
    verifier = secrets.token_urlsafe(_PKCE_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Return an unguessable ``state`` value for CSRF protection."""
    return secrets.token_urlsafe(_STATE_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class TokenResponse:
    """The subset of a token endpoint response this project uses.

    ``repr`` is overridden because the default dataclass repr would print both tokens into
    a log line the first time a caller forgets to redact it.
    """

    access_token: str
    refresh_token: str = ""
    expires_in: float = 0.0
    scope: str = ""
    token_type: str = ""

    def __repr__(self) -> str:
        refresh = "***" if self.refresh_token else ""
        return (
            "TokenResponse(access_token=***, "
            f"refresh_token={refresh!r}, expires_in={self.expires_in!r}, "
            f"token_type={self.token_type!r})"
        )


class LoopbackServer:
    """A single-use ``127.0.0.1`` callback listener for the authorization code flow.

    The socket is bound in ``__init__`` so :attr:`port` and :attr:`redirect_uri` are known
    before the authorization URL is built. :meth:`start` only begins serving in a daemon
    thread; :meth:`wait` blocks until the callback arrives, the provider returns an error,
    or the timeout expires. :meth:`close` is idempotent, so callers can use it in ``finally``
    and in :meth:`wait` without double-shutdown errors.
    """

    def __init__(
        self,
        *,
        state: str,
        port: int = 0,
        host: str = LOOPBACK_HOST,
        path: str = CALLBACK_PATH,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError("the OAuth loopback server must bind 127.0.0.1")
        if not path.startswith("/"):
            raise ValueError("the OAuth callback path must start with '/'")
        if port < 0 or port > 65535:
            raise ValueError("the OAuth callback port must be between 0 and 65535")
        if timeout <= 0:
            raise ValueError("the OAuth callback timeout must be positive")
        self._expected_state = state
        self._timeout = timeout
        self._server = _CallbackHTTPServer(
            (host, port),
            _CallbackHandler,
            expected_state=state,
            callback_path=path,
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    def __repr__(self) -> str:
        return f"LoopbackServer(redirect_uri={self.redirect_uri!r})"

    @property
    def port(self) -> int:
        """The bound port (the requested one, or the OS-assigned one when 0 was asked)."""
        address = cast(tuple[str, int], self._server.server_address)
        return int(address[1])

    @property
    def redirect_uri(self) -> str:
        """The exact redirect URI to send to the provider and repeat at the token endpoint."""
        return f"http://{LOOPBACK_HOST}:{self.port}{self._server.callback_path}"

    def start(self) -> None:
        """Serve callbacks in a daemon thread. Safe to call once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="lv24-seo-oauth-loopback",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> str:
        """Block for the callback and return the authorization code.

        Raises ``AUTH_INVALID`` when the provider reported an error, when ``state`` does not
        match this attempt, or when no code arrived; ``AUTH_REQUIRED`` on timeout.
        """
        deadline = self._timeout if timeout is None else timeout
        try:
            if not self._server.event.wait(deadline):
                raise SeoError(
                    code=AUTH_REQUIRED,
                    message=(f"timed out waiting for the OAuth callback on {self.redirect_uri}"),
                    next_action=(
                        "rerun the login and finish it in a browser on this machine, "
                        "or pass the authorization code with --code"
                    ),
                    retryable=True,
                )
            if self._server.error == "state_mismatch":
                raise SeoError(
                    code=AUTH_INVALID,
                    message="the OAuth callback state does not match this login attempt",
                    next_action="rerun the login and use only the URL it prints",
                    retryable=False,
                )
            if self._server.error:
                raise SeoError(
                    code=AUTH_INVALID,
                    message=(
                        "the provider returned an OAuth error: "
                        f"{_safe_provider_text(self._server.error)}"
                    ),
                    next_action="rerun the login and grant the requested scopes",
                    retryable=False,
                )
            if not self._server.code:
                raise SeoError(
                    code=AUTH_INVALID,
                    message="the OAuth callback carried no authorization code",
                    next_action="rerun the login and approve access in the browser",
                    retryable=False,
                )
            return self._server.code
        finally:
            self.close()

    def close(self) -> None:
        """Stop serving and release the port. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._server.event.set()
        if self._thread is not None:
            # shutdown() blocks until serve_forever() returns and deadlocks when the server
            # was never started, so it is tied to the thread that runs it.
            self._server.shutdown()
            self._thread.join(timeout=5.0)
        self._server.server_close()


def exchange_code(
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TOKEN_TIMEOUT_SECONDS,
    next_action: str = "",
) -> TokenResponse:
    """Exchange an authorization code for tokens at ``token_url``.

    ``client_secret`` is sent in the request body: Google's "Desktop app" clients issue a
    secret that is not confidential, and the token endpoint accepts the form field for both
    confidential and public clients.
    """
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _token_request(
        token_url,
        data,
        transport=transport,
        timeout=timeout,
        next_action=next_action,
    )


def refresh_access_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TOKEN_TIMEOUT_SECONDS,
    next_action: str = "",
) -> TokenResponse:
    """Exchange a refresh token for a new access token at ``token_url``."""
    return _token_request(
        token_url,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        transport=transport,
        timeout=timeout,
        next_action=next_action,
    )


def _token_request(
    token_url: str,
    data: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None,
    timeout: float,
    next_action: str,
) -> TokenResponse:
    target = _target(token_url)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), transport=transport) as client:
            response = client.post(token_url, data=dict(data))
    except httpx.HTTPError as exc:
        # Never interpolate ``exc``: httpx messages can embed the request body.
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"the OAuth token endpoint is unreachable ({target})",
            next_action=next_action or "check the network and retry",
            retryable=True,
            detail={"target": target, "error_type": type(exc).__name__},
        ) from None

    if response.is_error:
        provider_error = _provider_error(response)
        detail: dict[str, Any] = {"status_code": response.status_code, "target": target}
        if provider_error:
            detail["provider_error"] = provider_error
        if response.status_code == 429 or response.status_code >= 500:
            raise SeoError(
                code=UPSTREAM_UNAVAILABLE,
                message=(
                    f"the OAuth token endpoint is unavailable "
                    f"(HTTP {response.status_code}) for {target}"
                ),
                next_action=next_action or "retry the login in a moment",
                retryable=True,
                detail=detail,
            )
        raise SeoError(
            code=AUTH_INVALID,
            message=(
                f"the OAuth token request was rejected (HTTP {response.status_code}) for {target}"
            ),
            next_action=next_action or "rerun the login and grant the requested scopes",
            retryable=False,
            detail=detail,
        )

    try:
        payload: Any = response.json()
    except ValueError:
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"the OAuth token endpoint returned invalid JSON ({target})",
            next_action=next_action or "retry the login in a moment",
            retryable=True,
            detail={"status_code": response.status_code, "target": target},
        ) from None
    if not isinstance(payload, Mapping):
        raise SeoError(
            code=AUTH_INVALID,
            message="the OAuth token endpoint returned an unexpected response",
            next_action=next_action or "rerun the login",
            retryable=False,
            detail={"status_code": response.status_code, "target": target},
        )

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise SeoError(
            code=AUTH_INVALID,
            message="the OAuth token endpoint returned no access token",
            next_action=next_action or "rerun the login",
            retryable=False,
            detail={"status_code": response.status_code, "target": target},
        )
    return TokenResponse(
        access_token=access_token.strip(),
        refresh_token=_text(payload.get("refresh_token")),
        expires_in=_expires_in(payload.get("expires_in")),
        scope=_text(payload.get("scope")),
        token_type=_text(payload.get("token_type")),
    )


class _CallbackHTTPServer(ThreadingHTTPServer):
    """One-request HTTP server that records the OAuth callback parameters."""

    daemon_threads = True
    # A keep-alive handler thread must never delay close() after the code was captured.
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        expected_state: str,
        callback_path: str,
    ) -> None:
        super().__init__(address, handler)
        self.expected_state = expected_state
        self.callback_path = callback_path
        self.event = threading.Event()
        self.code = ""
        self.state = ""
        self.error = ""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handle ``GET /callback`` only; every other path is a 404."""

    server_version = "lv24-seo-oauth"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = cast(_CallbackHTTPServer, self.server)
        parsed = urlsplit(self.path)
        if parsed.path != server.callback_path:
            self._respond(404, _page("Not found", "This is not the OAuth callback path."))
            return

        params = parse_qs(parsed.query, keep_blank_values=True)
        state = _first(params, "state")
        code = _first(params, "code")
        error = _first(params, "error")

        if error:
            server.error = _safe_provider_text(error)
            server.state = state
            self._respond(
                400,
                _page(
                    "Authorization failed",
                    "The provider reported an error. Return to the terminal for details.",
                ),
            )
        elif state != server.expected_state:
            # Do not record the code: a mismatched state means the response is not ours.
            server.error = "state_mismatch"
            server.state = state
            self._respond(
                400,
                _page(
                    "Authorization failed",
                    "The state parameter does not match this login attempt.",
                ),
            )
        elif not code:
            server.error = "missing_code"
            server.state = state
            self._respond(
                400,
                _page("Authorization failed", "The callback carried no authorization code."),
            )
        else:
            server.code = code
            server.state = state
            self._respond(
                200,
                _page(
                    "Authorization complete",
                    "You can close this window and return to the terminal.",
                ),
            )
        server.event.set()

    def log_message(self, format: str, *args: Any) -> None:
        # The default implementation logs the request path, which carries the authorization
        # code and the state. Nothing from this server may reach a log file.
        return None

    def _respond(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)


def _page(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:4rem auto;max-width:32rem;"
        "padding:0 1rem;line-height:1.5}h1{font-size:1.4rem}</style></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )


def _first(params: Mapping[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0] if values else ""


def _safe_provider_text(value: str) -> str:
    """Keep a provider error code printable without echoing arbitrary content."""
    text = "".join(char for char in value.strip() if char.isprintable())
    return text[:100]


def _target(url: str) -> str:
    """Return ``host + path`` only: query strings and userinfo never reach an error."""
    parsed = urlsplit(url)
    return f"{parsed.hostname or '<unknown-host>'}{parsed.path or '/'}"


def _text(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _expires_in(raw: object) -> float:
    if isinstance(raw, bool):
        return 3600.0
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else 3600.0
    if isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return 3600.0
        return value if value > 0 else 3600.0
    return 3600.0


def _provider_error(response: httpx.Response) -> str:
    """Best-effort provider error code (``invalid_grant``); never the description."""
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    return _safe_provider_text(_text(payload.get("error")))
