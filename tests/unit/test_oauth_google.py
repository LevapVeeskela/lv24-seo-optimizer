"""Offline tests for the Google Search Console OAuth connector.

Every provider call is an ``httpx.MockTransport``; the only real socket is the loopback
callback server, which is bound to ``127.0.0.1`` by the test itself. No external network,
no real keyring (see ``tests/conftest.py``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from lv24_seo.cli import auth_google
from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import (
    AuthConfig,
    GscConfig,
    OAuthClientConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.oauth import (
    CALLBACK_PATH,
    LOOPBACK_HOST,
    LoopbackServer,
    TokenResponse,
    exchange_code,
    generate_pkce,
    generate_state,
    refresh_access_token,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.gsc.auth import (
    WEBMASTERS_READONLY_SCOPE,
    ServiceAccountTokenProvider,
    StaticTokenProvider,
)
from lv24_seo.providers.gsc.oauth import (
    ACCESS_TOKEN_SECRET,
    AUTHORIZATION_ENDPOINT,
    CLIENT_SECRET_NAME,
    CODE_VERIFIER_SECRET,
    EXPIRES_AT_SECRET,
    OAUTH_SOURCE,
    REFRESH_TOKEN_SECRET,
    TOKEN_ENDPOINT,
    OAuthTokenProvider,
    authorization_url,
)
from lv24_seo.services import SeoService

PROPERTY = "sc-domain:example.com"
CLIENT_ID = "1234567890.apps.googleusercontent.com"
CLIENT_SECRET = "desktop-client-secret"
REDIRECT_URI = "http://127.0.0.1:8123/callback"

RUNNER = CliRunner()


# -- helpers ----------------------------------------------------------------------------------


def _file_store(tmp_path: Path, **values: str) -> SecretStore:
    store = SecretStore(env={}, data_dir=tmp_path / "data", allow_file=True)
    for name, value in values.items():
        store.set(name, value)
    return store


def _config(tmp_path: Path, *, service_account_file: str = "") -> SeoConfig:
    return SeoConfig(
        providers=ProvidersConfig(
            gsc=GscConfig(property=PROPERTY, service_account_file=service_account_file)
        ),
        auth=AuthConfig(
            gsc=OAuthClientConfig(
                client_id=CLIENT_ID,
                scopes=[WEBMASTERS_READONLY_SCOPE],
                redirect_uri=REDIRECT_URI,
            )
        ),
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


def _expiry(now: float, seconds: float) -> str:
    return datetime.fromtimestamp(now + seconds, tz=UTC).isoformat(timespec="seconds")


def _token_transport(
    calls: list[httpx.Request],
    *,
    access_token: str = "ya29.fresh-access-token",
    refresh_token: str = "",
    expires_in: int = 3600,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload: dict[str, Any] = {
            "access_token": access_token,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
        if refresh_token:
            payload["refresh_token"] = refresh_token
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("utf-8"))


def _cli_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "LV24_SEO_DATA_DIR": str(tmp_path / "data"),
        "LV24_SEO_ALLOW_FILE_SECRETS": "1",
        "LV24_SEO_GSC_CLIENT_ID": CLIENT_ID,
        "LV24_SEO_GSC_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
        "LV24_SEO_GSC_REDIRECT_URI": REDIRECT_URI,
        "LV24_SEO_GSC_PROPERTY": PROPERTY,
        # Blank means "unset": a developer shell must not decide which credential wins.
        "LV24_SEO_GSC_TOKEN": "",
        "LV24_SEO_GSC_SERVICE_ACCOUNT_FILE": "",
    }
    env.update(overrides)
    return env


def _login_json(stdout: str) -> dict[str, Any]:
    """The last stdout line is the envelope; earlier lines are the printed consent URL."""
    payload: Any = json.loads(stdout.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


# -- PKCE and state --------------------------------------------------------------------------


def test_pkce_uses_s256_and_a_legal_verifier() -> None:
    verifier, challenge = generate_pkce()

    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected.rstrip(b"=").decode("ascii")
    assert generate_pkce()[0] != verifier


def test_state_is_unique_and_url_safe() -> None:
    first = generate_state()
    second = generate_state()

    assert first != second
    assert len(first) >= 32
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_authorization_url_carries_every_required_parameter() -> None:
    url = authorization_url(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scopes=[WEBMASTERS_READONLY_SCOPE],
        state="state-value",
        code_challenge="challenge-value",
    )

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)

    assert url.startswith(AUTHORIZATION_ENDPOINT)
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == [WEBMASTERS_READONLY_SCOPE]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["state"] == ["state-value"]
    assert params["code_challenge"] == ["challenge-value"]
    assert params["code_challenge_method"] == ["S256"]


# -- loopback callback -----------------------------------------------------------------------


def test_loopback_captures_the_code_and_serves_a_close_page() -> None:
    server = LoopbackServer(state="expected-state", port=0)
    try:
        assert server.port > 0
        assert server.redirect_uri == f"http://127.0.0.1:{server.port}{CALLBACK_PATH}"
        server.start()

        response = httpx.get(
            f"{server.redirect_uri}?code=the-code&state=expected-state", timeout=5.0
        )

        assert response.status_code == 200
        assert "close this window" in response.text
        assert server.wait(timeout=5.0) == "the-code"
    finally:
        server.close()


def test_loopback_rejects_a_mismatched_state() -> None:
    server = LoopbackServer(state="expected-state", port=0)
    try:
        server.start()
        response = httpx.get(f"{server.redirect_uri}?code=attacker&state=other", timeout=5.0)

        assert response.status_code == 400
        with pytest.raises(SeoError) as excinfo:
            server.wait(timeout=5.0)
        assert excinfo.value.code == AUTH_INVALID
        assert "state" in excinfo.value.message
    finally:
        server.close()


def test_loopback_reports_a_provider_error() -> None:
    server = LoopbackServer(state="expected-state", port=0)
    try:
        server.start()
        response = httpx.get(
            f"{server.redirect_uri}?error=access_denied&state=expected-state", timeout=5.0
        )

        assert response.status_code == 400
        with pytest.raises(SeoError) as excinfo:
            server.wait(timeout=5.0)
        assert excinfo.value.code == AUTH_INVALID
        assert "access_denied" in excinfo.value.message
    finally:
        server.close()


def test_loopback_ignores_other_paths() -> None:
    server = LoopbackServer(state="expected-state", port=0)
    try:
        server.start()
        response = httpx.get(f"http://127.0.0.1:{server.port}/not-the-callback", timeout=5.0)

        assert response.status_code == 404
        with pytest.raises(SeoError) as excinfo:
            server.wait(timeout=0.1)
        assert excinfo.value.code == AUTH_REQUIRED
    finally:
        server.close()


def test_loopback_refuses_to_bind_a_public_interface() -> None:
    with pytest.raises(ValueError):
        LoopbackServer(state="s", host="0.0.0.0")


def test_loopback_timeout_raises_auth_required() -> None:
    server = LoopbackServer(state="expected-state", port=0)
    try:
        server.start()
        with pytest.raises(SeoError) as excinfo:
            server.wait(timeout=0.1)
        assert excinfo.value.code == AUTH_REQUIRED
        assert excinfo.value.retryable is True
    finally:
        server.close()


def test_loopback_host_is_always_loopback() -> None:
    server = LoopbackServer(state="s", port=0)
    try:
        assert LOOPBACK_HOST == "127.0.0.1"
        assert server.redirect_uri.startswith("http://127.0.0.1:")
    finally:
        server.close()


# -- token exchange --------------------------------------------------------------------------


def test_exchange_code_posts_the_pkce_verifier_and_parses_tokens() -> None:
    calls: list[httpx.Request] = []
    transport = _token_transport(calls, refresh_token="1//refresh-token")

    response = exchange_code(
        TOKEN_ENDPOINT,
        CLIENT_ID,
        CLIENT_SECRET,
        "auth-code",
        REDIRECT_URI,
        "verifier-value",
        transport=transport,
    )

    assert response.access_token == "ya29.fresh-access-token"
    assert response.refresh_token == "1//refresh-token"
    assert response.expires_in == 3600.0
    assert len(calls) == 1
    form = _form(calls[0])
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert form["code"] == ["auth-code"]
    assert form["redirect_uri"] == [REDIRECT_URI]
    assert form["code_verifier"] == ["verifier-value"]


def test_exchange_code_omits_an_empty_verifier() -> None:
    calls: list[httpx.Request] = []
    exchange_code(
        TOKEN_ENDPOINT,
        CLIENT_ID,
        CLIENT_SECRET,
        "auth-code",
        REDIRECT_URI,
        "",
        transport=_token_transport(calls),
    )

    assert "code_verifier" not in _form(calls[0])


def test_refresh_access_token_posts_the_refresh_grant() -> None:
    calls: list[httpx.Request] = []
    response = refresh_access_token(
        TOKEN_ENDPOINT,
        CLIENT_ID,
        CLIENT_SECRET,
        "1//refresh-token",
        transport=_token_transport(calls, access_token="ya29.refreshed"),
    )

    assert response.access_token == "ya29.refreshed"
    form = _form(calls[0])
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["1//refresh-token"]
    assert form["client_secret"] == [CLIENT_SECRET]


def test_exchange_maps_invalid_grant_to_auth_invalid_without_echoing_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "Bad Request: secret-code"},
        )

    with pytest.raises(SeoError) as excinfo:
        exchange_code(
            TOKEN_ENDPOINT,
            CLIENT_ID,
            CLIENT_SECRET,
            "secret-code",
            REDIRECT_URI,
            "verifier",
            transport=httpx.MockTransport(handler),
        )

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert error.retryable is False
    assert error.detail["provider_error"] == "invalid_grant"
    assert "secret-code" not in error.message
    assert "secret-code" not in json.dumps(error.detail)


def test_exchange_maps_5xx_to_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="backend unavailable")

    with pytest.raises(SeoError) as excinfo:
        exchange_code(
            TOKEN_ENDPOINT,
            CLIENT_ID,
            CLIENT_SECRET,
            "code",
            REDIRECT_URI,
            "verifier",
            transport=httpx.MockTransport(handler),
        )

    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_exchange_rejects_a_response_without_an_access_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    with pytest.raises(SeoError) as excinfo:
        exchange_code(
            TOKEN_ENDPOINT,
            CLIENT_ID,
            CLIENT_SECRET,
            "code",
            REDIRECT_URI,
            "verifier",
            transport=httpx.MockTransport(handler),
        )

    assert excinfo.value.code == AUTH_INVALID


def test_token_response_repr_hides_both_tokens() -> None:
    response = TokenResponse(
        access_token="ya29.super-secret-access",
        refresh_token="1//super-secret-refresh",
        expires_in=3600,
    )

    text = repr(response)

    assert "ya29.super-secret-access" not in text
    assert "1//super-secret-refresh" not in text
    assert "***" in text


# -- OAuthTokenProvider ----------------------------------------------------------------------


def test_provider_returns_a_stored_token_that_is_not_near_expiry(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    store = _file_store(
        tmp_path,
        gsc_access_token="ya29.stored",
        gsc_refresh_token="1//refresh",
        gsc_token_expires_at=_expiry(now, 3600),
    )
    calls: list[httpx.Request] = []
    provider = OAuthTokenProvider(
        store=store,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        clock=lambda: now,
        transport=_token_transport(calls),
    )

    assert provider.access_token() == "ya29.stored"
    assert calls == []


def test_provider_refreshes_within_the_margin_and_persists_the_grant(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    store = _file_store(
        tmp_path,
        gsc_access_token="ya29.expiring",
        gsc_refresh_token="1//refresh",
        gsc_token_expires_at=_expiry(now, 30),
    )
    calls: list[httpx.Request] = []
    provider = OAuthTokenProvider(
        store=store,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        clock=lambda: now,
        transport=_token_transport(calls, access_token="ya29.refreshed"),
    )

    assert provider.access_token() == "ya29.refreshed"
    assert len(calls) == 1
    assert _form(calls[0])["refresh_token"] == ["1//refresh"]
    assert store.get(ACCESS_TOKEN_SECRET) == "ya29.refreshed"
    assert store.get(REFRESH_TOKEN_SECRET) == "1//refresh"
    assert store.get(EXPIRES_AT_SECRET) == _expiry(now, 3600)


def test_provider_requires_a_refresh_token(tmp_path: Path) -> None:
    store = _file_store(tmp_path, gsc_access_token="ya29.expiring")
    provider = OAuthTokenProvider(
        store=store,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        clock=lambda: 1_800_000_000.0,
    )

    with pytest.raises(SeoError) as excinfo:
        provider.access_token()

    assert excinfo.value.code == AUTH_REQUIRED
    assert "auth login" in (excinfo.value.next_action or "")


def test_provider_repr_hides_the_secret(tmp_path: Path) -> None:
    provider = OAuthTokenProvider(
        store=_file_store(tmp_path),
        client_id=CLIENT_ID,
        client_secret="super-secret-value",
    )

    assert "super-secret-value" not in repr(provider)
    assert "***" in repr(provider)


# -- service precedence ----------------------------------------------------------------------


def test_service_prefers_oauth_over_a_broken_service_account_file(tmp_path: Path) -> None:
    store = _file_store(
        tmp_path,
        gsc_refresh_token="1//refresh",
        gsc_oauth_client_secret=CLIENT_SECRET,
    )
    service = SeoService(
        _config(tmp_path, service_account_file=str(tmp_path / "missing.json")),
        secrets=store,
    )

    client = service._maybe_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, OAuthTokenProvider)


def test_service_ignores_an_incomplete_oauth_grant(tmp_path: Path) -> None:
    store = _file_store(tmp_path, gsc_refresh_token="1//refresh")  # no client secret
    service = SeoService(_config(tmp_path), secrets=store)

    # Without a client secret the incomplete grant must not win; the static token is next.
    assert service._maybe_client("gsc") is None

    store.set("gsc_token", "ya29.static")
    client = service._maybe_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, StaticTokenProvider)


def test_service_uses_the_static_token_when_no_oauth_grant_exists(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path),
        secrets=_file_store(tmp_path, gsc_token="ya29.static"),
    )

    client = service._wired_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, StaticTokenProvider)


def test_properties_reports_the_oauth_source(tmp_path: Path) -> None:
    store = _file_store(
        tmp_path,
        gsc_refresh_token="1//refresh",
        gsc_oauth_client_secret=CLIENT_SECRET,
    )
    service = SeoService(_config(tmp_path), secrets=store)

    response = service.properties()

    entries = {entry["provider"]: entry for entry in response["data"]["providers"]}
    assert entries["gsc"]["token_present"] is True
    assert entries["gsc"]["token_source"] == OAUTH_SOURCE
    assert entries["gsc"]["ready"] is True
    assert "1//refresh" not in json.dumps(response)


def test_service_account_path_still_builds_a_service_account_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = object()
    monkeypatch.setattr("lv24_seo.services.load_service_account_file", lambda raw: marker)
    service = SeoService(
        _config(tmp_path, service_account_file=str(tmp_path / "key.json")),
        secrets=_file_store(tmp_path),
    )

    client = service._build_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, ServiceAccountTokenProvider)


# -- CLI -------------------------------------------------------------------------------------


def test_cli_login_with_code_stores_tokens_and_prints_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str]] = []

    def fake_exchange(
        token_url: str,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        **kwargs: Any,
    ) -> TokenResponse:
        calls.append(
            {
                "token_url": token_url,
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        return TokenResponse(
            access_token="ya29.cli-access",
            refresh_token="1//cli-refresh",
            expires_in=3600,
        )

    monkeypatch.setattr(auth_google, "exchange_code", fake_exchange)
    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "login", "--provider", "gsc", "--code", "auth-code", "--json"],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    payload = _login_json(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["provider"] == "gsc"
    assert payload["data"]["flow"] == "oauth"
    assert payload["data"]["scopes"] == [WEBMASTERS_READONLY_SCOPE]
    assert payload["data"]["target"] == PROPERTY
    assert payload["data"]["expires_at"]
    assert calls == [
        {
            "token_url": TOKEN_ENDPOINT,
            "client_id": CLIENT_ID,
            "code": "auth-code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": "",
        }
    ]
    assert "ya29.cli-access" not in result.stdout
    assert "1//cli-refresh" not in result.stdout

    store = SecretStore(env={}, data_dir=tmp_path / "data", allow_file=True)
    assert store.get(ACCESS_TOKEN_SECRET) == "ya29.cli-access"
    assert store.get(REFRESH_TOKEN_SECRET) == "1//cli-refresh"
    assert store.get(EXPIRES_AT_SECRET)
    assert store.get(CODE_VERIFIER_SECRET) is None


def test_cli_login_without_browser_prints_the_url_and_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers: list[Any] = []

    class FakeServer:
        def __init__(self, *, state: str, port: int, timeout: float) -> None:
            self.state = state
            self.requested_port = port
            self.timeout = timeout
            self.redirect_uri = REDIRECT_URI
            self.started = False
            self.closed = False
            servers.append(self)

        def start(self) -> None:
            self.started = True

        def wait(self, timeout: float | None = None) -> str:
            return "callback-code"

        def close(self) -> None:
            self.closed = True

    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    captured: list[str] = []

    def fake_exchange(
        token_url: str,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        **kwargs: Any,
    ) -> TokenResponse:
        captured.append(code)
        return TokenResponse(access_token="ya29.browser-access", expires_in=3600)

    monkeypatch.setattr(auth_google, "LoopbackServer", FakeServer)
    monkeypatch.setattr(auth_google.webbrowser, "open", fake_open)
    monkeypatch.setattr(auth_google, "exchange_code", fake_exchange)

    result = RUNNER.invoke(
        cli_main.app,
        [
            "auth",
            "login",
            "--provider",
            "gsc",
            "--no-browser",
            "--port",
            "8123",
            "--json",
        ],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    payload = _login_json(result.stdout)
    assert payload["data"]["flow"] == "oauth"
    output = result.stdout + getattr(result, "stderr", "")
    assert AUTHORIZATION_ENDPOINT in output
    assert "code_challenge_method=S256" in output
    assert "state=" in output
    assert opened == []
    assert captured == ["callback-code"]
    assert servers and servers[0].started and servers[0].closed
    assert servers[0].requested_port == 8123
    assert "ya29.browser-access" not in output


def test_cli_login_requires_the_client_secret(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    env.pop("LV24_SEO_GSC_OAUTH_CLIENT_SECRET")

    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "login", "--provider", "gsc", "--code", "x", "--json"],
        env=env,
    )

    assert result.exit_code == 3
    payload = _login_json(result.stdout)
    assert payload["error"]["code"] == AUTH_REQUIRED
    assert "gsc_oauth_secret" in payload["error"]["next_action"]


def test_cli_login_with_code_needs_a_redirect_uri(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    env.pop("LV24_SEO_GSC_REDIRECT_URI")

    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "login", "--provider", "gsc", "--code", "x", "--json"],
        env=env,
    )

    assert result.exit_code == 2
    payload = _login_json(result.stdout)
    assert payload["error"]["code"] == INVALID_INPUT
    assert "--port" in payload["error"]["next_action"]


def test_cli_logout_removes_tokens_and_keeps_the_client_secret(tmp_path: Path) -> None:
    store = SecretStore(env={}, data_dir=tmp_path / "data", allow_file=True)
    store.set(ACCESS_TOKEN_SECRET, "ya29.access")
    store.set(REFRESH_TOKEN_SECRET, "1//refresh")
    store.set(EXPIRES_AT_SECRET, "2026-11-18T00:00:00+00:00")
    store.set(CODE_VERIFIER_SECRET, "verifier")
    store.set(CLIENT_SECRET_NAME, CLIENT_SECRET)

    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "logout", "--provider", "gsc", "--json"],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    payload = _login_json(result.stdout)
    assert payload["data"]["deleted"] is True
    assert store.get(ACCESS_TOKEN_SECRET) is None
    assert store.get(REFRESH_TOKEN_SECRET) is None
    assert store.get(EXPIRES_AT_SECRET) is None
    assert store.get(CODE_VERIFIER_SECRET) is None
    assert store.get(CLIENT_SECRET_NAME) == CLIENT_SECRET


def test_cli_auth_status_accepts_the_oauth_secret_names(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "status", "--json"],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    payload = _login_json(result.stdout)
    providers = {entry["provider"]: entry for entry in payload["data"]["providers"]}
    assert providers["gsc_oauth"]["secret"] == "gsc_refresh_token"
    assert providers["gsc_oauth_secret"]["secret"] == "gsc_oauth_client_secret"
    assert providers["gsc_oauth_secret"]["present"] is True
    assert "client-secret" not in result.stdout


def test_login_expiry_is_iso_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_google,
        "exchange_code",
        lambda *args, **kwargs: TokenResponse(access_token="a", expires_in=3600),
    )

    result = RUNNER.invoke(
        cli_main.app,
        ["auth", "login", "--provider", "gsc", "--code", "c", "--json"],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    expires_at = _login_json(result.stdout)["data"]["expires_at"]
    parsed = datetime.fromisoformat(expires_at)
    assert parsed.tzinfo is not None
    assert parsed > datetime.now(UTC) + timedelta(minutes=30)


def test_doctor_reports_the_oauth_grant(tmp_path: Path) -> None:
    store = SecretStore(env={}, data_dir=tmp_path / "data", allow_file=True)
    store.set(REFRESH_TOKEN_SECRET, "1//refresh")
    store.set(CLIENT_SECRET_NAME, CLIENT_SECRET)
    config_path = tmp_path / "seo.toml"
    config_path.write_text(
        f'[auth.gsc]\nclient_id = "{CLIENT_ID}"\n[providers.gsc]\nproperty = "{PROPERTY}"\n',
        encoding="utf-8",
    )

    result = RUNNER.invoke(
        cli_main.app,
        ["doctor", "--config", str(config_path), "--json"],
        env=_cli_env(tmp_path),
    )

    assert result.exit_code == 0, result.stdout
    checks = {check["id"]: check for check in _login_json(result.stdout)["data"]["checks"]}
    assert checks["gsc_auth"]["ok"] is True
    assert "OAuth refresh token" in checks["gsc_auth"]["detail"]
