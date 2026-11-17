"""Offline tests for the Yandex OAuth connector (``httpx.MockTransport``; no network)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from lv24_seo import services as _services_module
from lv24_seo.cli import auth as cli_auth
from lv24_seo.cli import auth_yandex
from lv24_seo.cli.main import app
from lv24_seo.core.config import (
    AuthConfig,
    OAuthClientConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
    YandexMetrikaConfig,
    YandexWebmasterConfig,
)
from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    CONFIG_INVALID,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.core.oauth import is_placeholder_client_id
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.yandex_oauth import (
    AUTHORIZE_URL,
    TOKEN_URL,
    VERIFICATION_CODE_REDIRECT_URI,
    OAuthTokens,
    authorization_url,
    ensure_fresh_access_token,
    exchange_code,
    provider_secret_names,
    refresh_token,
    store_tokens,
)
from lv24_seo.services import SeoService

RUNNER = CliRunner()
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CLIENT_ID = "0123456789abcdef0123456789abcdef"
CLIENT_SECRET = "yandex-client-secret"
ACCESS = "access-token-value"
REFRESH = "refresh-token-value"

Handler = Callable[[httpx.Request], httpx.Response]


class MemoryStore:
    """In-memory stand-in for :class:`SecretStore` with the same read/write surface."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def _clock() -> datetime:
    return NOW


def _http(handler: Handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)


def _json_handler(
    payload: object,
    *,
    status: int = 200,
) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    return handler, seen


def _config(
    *,
    client_id: str = CLIENT_ID,
    host_id: str = "https:example.com:443",
    counter: str = "123456",
) -> SeoConfig:
    return SeoConfig(
        providers=ProvidersConfig(
            yandex_webmaster=YandexWebmasterConfig(host_id=host_id) if host_id else None,
            yandex_metrika=YandexMetrikaConfig(counter=counter) if counter else None,
        ),
        auth=AuthConfig(
            yandex=OAuthClientConfig(
                client_id=client_id,
                scopes=["webmaster:hostinfo", "metrika:read"],
            )
        ),
        storage=StorageConfig(data_dir=Path("data")),
    )


def _expiry(delta: timedelta) -> str:
    return (NOW + delta).isoformat(timespec="seconds")


def test_authorization_url_contains_scopes_state_and_pkce() -> None:
    url = authorization_url(
        CLIENT_ID,
        ["webmaster:hostinfo", "metrika:read"],
        "state-value",
        code_challenge="pkce-challenge",
    )

    assert url.startswith(f"{AUTHORIZE_URL}?")
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["scope"] == ["webmaster:hostinfo metrika:read"]
    assert query["state"] == ["state-value"]
    assert query["redirect_uri"] == [VERIFICATION_CODE_REDIRECT_URI]
    assert query["code_challenge"] == ["pkce-challenge"]
    assert query["code_challenge_method"] == ["S256"]


def test_authorization_url_omits_pkce_and_rejects_empty_values() -> None:
    query = parse_qs(urlsplit(authorization_url(CLIENT_ID, ["metrika:read"], "s")).query)
    assert "code_challenge" not in query
    assert "code_challenge_method" not in query

    with pytest.raises(SeoError) as excinfo:
        authorization_url("", ["metrika:read"], "s")
    assert excinfo.value.code == AUTH_REQUIRED
    with pytest.raises(SeoError) as excinfo:
        authorization_url(CLIENT_ID, ["metrika:read"], "  ")
    assert excinfo.value.code == INVALID_INPUT


def test_exchange_code_posts_form_encoded_and_parses_tokens() -> None:
    handler, seen = _json_handler(
        {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "expires_in": 3600,
            "scope": "webmaster:hostinfo metrika:read",
            "token_type": "bearer",
        }
    )

    tokens = exchange_code(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        code="the-code",
        http=_http(handler),
        now=_clock,
    )

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = parse_qs(request.content.decode("utf-8"))
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    assert body["client_id"] == [CLIENT_ID]
    assert body["client_secret"] == [CLIENT_SECRET]
    assert body["redirect_uri"] == [VERIFICATION_CODE_REDIRECT_URI]

    assert tokens.access_token == ACCESS
    assert tokens.refresh_token == REFRESH
    assert tokens.expires_in == 3600
    assert tokens.scope == ("webmaster:hostinfo", "metrika:read")
    assert tokens.expires_at == NOW + timedelta(seconds=3600)
    assert ACCESS not in repr(tokens)
    assert REFRESH not in repr(tokens)


def test_exchange_code_maps_provider_error_without_echoing_the_body() -> None:
    handler, _ = _json_handler(
        {
            "error": "invalid_grant",
            "error_description": "Code has expired: super-secret-code",
        },
        status=400,
    )

    with pytest.raises(SeoError) as excinfo:
        exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="the-code",
            http=_http(handler),
            now=_clock,
        )

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert error.detail["error"] == "invalid_grant"
    assert "super-secret-code" not in str(error)
    assert "super-secret-code" not in json.dumps(error.detail)
    assert "the-code" not in str(error)


def test_exchange_code_maps_5xx_to_upstream_unavailable() -> None:
    handler, _ = _json_handler({"error": "server_error"}, status=503)

    with pytest.raises(SeoError) as excinfo:
        exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="the-code",
            http=_http(handler),
            now=_clock,
        )

    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_refresh_token_uses_the_refresh_grant() -> None:
    handler, seen = _json_handler({"access_token": "new-access", "expires_in": "7200"})

    tokens = refresh_token(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH,
        http=_http(handler),
        now=_clock,
    )

    body = parse_qs(seen[0].content.decode("utf-8"))
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == [REFRESH]
    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == ""
    assert tokens.expires_in == 7200
    assert tokens.expires_at == NOW + timedelta(seconds=7200)


def test_ensure_returns_a_valid_token_without_a_request() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": ACCESS,
            "yandex_token_expires_at": _expiry(timedelta(hours=1)),
        }
    )

    def fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a valid token must not trigger a refresh")

    token = ensure_fresh_access_token(
        store, "yandex", client_id=CLIENT_ID, http=_http(fail), now=_clock
    )

    assert token == ACCESS


def test_ensure_refreshes_an_expired_token_and_persists_it() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "old-access",
            "yandex_refresh_token": "old-refresh",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
            "yandex_oauth_client_secret": CLIENT_SECRET,
        }
    )
    handler, seen = _json_handler({"access_token": "new-access", "expires_in": 3600})

    token = ensure_fresh_access_token(
        store, "yandex", client_id=CLIENT_ID, http=_http(handler), now=_clock
    )

    assert token == "new-access"
    assert store.values["yandex_webmaster_token"] == "new-access"
    # Yandex may omit a new refresh token; the old one must survive.
    assert store.values["yandex_refresh_token"] == "old-refresh"
    assert datetime.fromisoformat(store.values["yandex_token_expires_at"]) > NOW
    body = parse_qs(seen[0].content.decode("utf-8"))
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["old-refresh"]


def test_ensure_prefers_refresh_when_the_expiry_is_unknown() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "static-access",
            "yandex_refresh_token": "old-refresh",
            "yandex_oauth_client_secret": CLIENT_SECRET,
        }
    )
    handler, seen = _json_handler({"access_token": "new-access", "expires_in": 3600})

    token = ensure_fresh_access_token(
        store, "yandex", client_id=CLIENT_ID, http=_http(handler), now=_clock
    )

    assert token == "new-access"
    assert len(seen) == 1


def test_ensure_keeps_a_long_lived_static_token_without_a_refresh_token() -> None:
    store = MemoryStore({"yandex_webmaster_token": "static-access"})

    def fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a static token must not trigger a refresh")

    token = ensure_fresh_access_token(
        store, "yandex", client_id=CLIENT_ID, http=_http(fail), now=_clock
    )

    assert token == "static-access"


def test_ensure_returns_a_still_valid_token_inside_the_margin_without_refresh() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "still-valid",
            "yandex_token_expires_at": _expiry(timedelta(seconds=30)),
        }
    )

    def fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a static token must not trigger a refresh")

    token = ensure_fresh_access_token(
        store, "yandex", client_id=CLIENT_ID, http=_http(fail), now=_clock
    )

    assert token == "still-valid"


def test_ensure_expired_without_a_refresh_token_is_auth_invalid() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "old-access",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
        }
    )

    with pytest.raises(SeoError) as excinfo:
        ensure_fresh_access_token(store, "yandex", client_id=CLIENT_ID, now=_clock)

    assert excinfo.value.code == AUTH_INVALID
    assert "auth login --provider yandex" in (excinfo.value.next_action or "")


def test_ensure_missing_token_is_auth_required() -> None:
    with pytest.raises(SeoError) as excinfo:
        ensure_fresh_access_token(MemoryStore(), "yandex", client_id=CLIENT_ID, now=_clock)

    assert excinfo.value.code == AUTH_REQUIRED


def test_ensure_refresh_failure_is_auth_invalid_with_login_action() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "old-access",
            "yandex_refresh_token": "old-refresh",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
            "yandex_oauth_client_secret": CLIENT_SECRET,
        }
    )
    handler, _ = _json_handler({"error": "invalid_grant"}, status=400)

    with pytest.raises(SeoError) as excinfo:
        ensure_fresh_access_token(
            store, "yandex", client_id=CLIENT_ID, http=_http(handler), now=_clock
        )

    assert excinfo.value.code == AUTH_INVALID
    assert "auth login --provider yandex" in (excinfo.value.next_action or "")
    assert store.values["yandex_webmaster_token"] == "old-access"


def test_ensure_refresh_without_a_client_secret_is_auth_invalid() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "old-access",
            "yandex_refresh_token": "old-refresh",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
        }
    )

    with pytest.raises(SeoError) as excinfo:
        ensure_fresh_access_token(store, "yandex", client_id=CLIENT_ID, now=_clock)

    assert excinfo.value.code == AUTH_INVALID
    assert "yandex_oauth_secret" in (excinfo.value.next_action or "")


def test_ensure_metrika_uses_metrika_secret_names() -> None:
    store = MemoryStore(
        {
            "yandex_metrika_token": "old-access",
            "yandex_metrika_refresh_token": "old-refresh",
            "yandex_metrika_token_expires_at": _expiry(timedelta(hours=-1)),
            "yandex_oauth_client_secret": CLIENT_SECRET,
        }
    )
    handler, _ = _json_handler({"access_token": "new-access", "expires_in": 3600})

    token = ensure_fresh_access_token(
        store, "metrika", client_id=CLIENT_ID, http=_http(handler), now=_clock
    )

    assert token == "new-access"
    assert store.values["yandex_metrika_token"] == "new-access"
    assert "yandex_webmaster_token" not in store.values
    assert "yandex_token_expires_at" not in store.values


def test_store_tokens_keeps_the_previous_refresh_token_and_clears_stale_expiry() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "old",
            "yandex_refresh_token": "old-refresh",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
        }
    )

    store_tokens(store, "yandex", OAuthTokens(access_token="new-access"))

    assert store.values["yandex_webmaster_token"] == "new-access"
    assert store.values["yandex_refresh_token"] == "old-refresh"
    assert "yandex_token_expires_at" not in store.values


def test_provider_secret_names_accept_aliases_and_reject_unknown() -> None:
    assert provider_secret_names("yandex_webmaster") == provider_secret_names("yandex")
    assert provider_secret_names("yandex_metrika") == provider_secret_names("metrika")
    with pytest.raises(SeoError) as excinfo:
        provider_secret_names("google")
    assert excinfo.value.code == INVALID_INPUT


def test_oauth_secret_names_are_accepted_by_the_secret_store(tmp_path: Path) -> None:
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)
    store.set("yandex_refresh_token", "refresh-value")

    assert store.get("yandex_refresh_token") == "refresh-value"
    assert store.status(["yandex_refresh_token"])[0].present is True


def test_login_requires_a_client_id() -> None:
    with pytest.raises(SeoError) as excinfo:
        auth_yandex.login_yandex(
            "yandex",
            _config(client_id=""),
            code="the-code",
            store=MemoryStore(),
        )

    assert excinfo.value.code == AUTH_REQUIRED
    assert "client_id" in (excinfo.value.next_action or "")


def test_login_requires_the_client_secret() -> None:
    with pytest.raises(SeoError) as excinfo:
        auth_yandex.login_yandex(
            "yandex",
            _config(),
            code="the-code",
            store=MemoryStore(),
        )

    assert excinfo.value.code == AUTH_REQUIRED
    assert "auth set --provider yandex_oauth_secret --stdin" in (excinfo.value.next_action or "")


def test_login_rejects_a_placeholder_client_id() -> None:
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})

    with pytest.raises(SeoError) as excinfo:
        auth_yandex.login_yandex(
            "yandex",
            _config(client_id="yandex-client-id"),
            url_only=True,
            store=store,
        )

    assert excinfo.value.code == CONFIG_INVALID
    assert "placeholder" in excinfo.value.message
    assert "oauth.yandex.ru" in (excinfo.value.next_action or "")


def test_placeholder_client_ids_are_rejected_by_the_helper() -> None:
    assert is_placeholder_client_id("yandex-client-id")
    assert is_placeholder_client_id("gsc-client-id")
    assert not is_placeholder_client_id(CLIENT_ID)


def test_login_url_only_returns_the_authorization_url() -> None:
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})

    result = auth_yandex.login_yandex("yandex", _config(), url_only=True, store=store)

    assert result["provider"] == "yandex"
    assert result["flow"] == "oauth"
    assert result["authorization_url"].startswith(AUTHORIZE_URL)
    assert "access_token" not in result
    assert "yandex_webmaster_token" not in store.values


def test_login_without_a_code_and_no_tty_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(auth_yandex.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})

    with pytest.raises(SeoError) as excinfo:
        auth_yandex.login_yandex(
            "yandex",
            _config(),
            no_browser=True,
            store=store,
        )

    assert excinfo.value.code == INVALID_INPUT
    assert "--code" in (excinfo.value.next_action or "")
    assert AUTHORIZE_URL in capsys.readouterr().err


def test_login_no_browser_prints_the_url_and_stores_tokens(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})
    captured: dict[str, Any] = {}

    def fake_exchange(**kwargs: Any) -> OAuthTokens:
        captured.update(kwargs)
        return OAuthTokens(
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_in=3600,
            scope=("webmaster:hostinfo", "metrika:read"),
            expires_at=NOW + timedelta(seconds=3600),
        )

    monkeypatch.setattr(auth_yandex, "exchange_code", fake_exchange)

    payload = auth_yandex.login_yandex(
        "yandex",
        _config(),
        code="the-code",
        no_browser=True,
        store=store,
    )

    assert captured["code"] == "the-code"
    assert captured["client_id"] == CLIENT_ID
    assert captured["client_secret"] == CLIENT_SECRET
    assert payload["provider"] == "yandex"
    assert payload["flow"] == "oauth"
    assert payload["scopes"] == ["webmaster:hostinfo", "metrika:read"]
    assert payload["target"] == "https:example.com:443"
    assert payload["expires_at"] == (NOW + timedelta(seconds=3600)).isoformat(timespec="seconds")
    assert AUTHORIZE_URL in capsys.readouterr().err

    assert store.values["yandex_webmaster_token"] == ACCESS
    assert store.values["yandex_refresh_token"] == REFRESH
    assert "yandex_token_expires_at" in store.values
    rendered = json.dumps(payload)
    assert ACCESS not in rendered
    assert REFRESH not in rendered
    assert CLIENT_SECRET not in rendered


def test_login_without_a_refresh_token_removes_the_previous_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        {
            "yandex_oauth_client_secret": CLIENT_SECRET,
            "yandex_refresh_token": "old-refresh",
            "yandex_token_expires_at": _expiry(timedelta(hours=-1)),
        }
    )
    monkeypatch.setattr(
        auth_yandex,
        "exchange_code",
        lambda **_kwargs: OAuthTokens(access_token="long-lived-access"),
    )

    auth_yandex.login_yandex("yandex", _config(), code="the-code", store=store)

    assert store.values["yandex_webmaster_token"] == "long-lived-access"
    assert "yandex_refresh_token" not in store.values
    assert "yandex_token_expires_at" not in store.values


def test_login_opens_the_browser_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(auth_yandex.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        auth_yandex,
        "exchange_code",
        lambda **_kwargs: OAuthTokens(access_token=ACCESS),
    )
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})

    payload = auth_yandex.login_yandex(
        "metrika",
        _config(),
        code="the-code",
        store=store,
    )

    assert opened and opened[0].startswith(f"{AUTHORIZE_URL}?")
    assert "authorization_url" not in payload
    assert store.values["yandex_metrika_token"] == ACCESS


def test_logout_removes_only_the_selected_provider_keys() -> None:
    store = MemoryStore(
        {
            "yandex_webmaster_token": "a",
            "yandex_refresh_token": "r",
            "yandex_token_expires_at": _expiry(timedelta(hours=1)),
            "yandex_metrika_token": "ma",
            "yandex_metrika_refresh_token": "mr",
            "yandex_metrika_token_expires_at": _expiry(timedelta(hours=1)),
            "yandex_oauth_client_secret": CLIENT_SECRET,
        }
    )

    auth_yandex.logout_yandex(store, "yandex")

    assert "yandex_webmaster_token" not in store.values
    assert "yandex_refresh_token" not in store.values
    assert "yandex_token_expires_at" not in store.values
    assert store.values["yandex_metrika_token"] == "ma"
    assert store.values["yandex_metrika_refresh_token"] == "mr"
    assert store.values["yandex_oauth_client_secret"] == CLIENT_SECRET


def test_cli_auth_login_yandex_no_browser_with_code(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore({"yandex_oauth_client_secret": CLIENT_SECRET})
    monkeypatch.setattr(cli_auth, "load_config", lambda _path=None: _config())
    monkeypatch.setattr(auth_yandex, "SecretStore", lambda **_kwargs: store)
    monkeypatch.setattr(
        auth_yandex,
        "exchange_code",
        lambda **_kwargs: OAuthTokens(
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_in=3600,
            expires_at=NOW + timedelta(seconds=3600),
        ),
    )

    result = RUNNER.invoke(
        app,
        [
            "auth",
            "login",
            "--provider",
            "yandex",
            "--code",
            "the-code",
            "--no-browser",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    stdout = getattr(result, "stdout", "") or result.output
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["data"]["flow"] == "oauth"
    assert payload["data"]["provider"] == "yandex"
    assert set(payload["data"]) == {"provider", "flow", "scopes", "expires_at", "target"}
    assert store.values["yandex_webmaster_token"] == ACCESS
    assert ACCESS not in result.output
    assert REFRESH not in result.output


def test_service_uses_the_refreshed_token_for_yandex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_ensure(
        store: Any,
        provider: str,
        *,
        client_id: str = "",
        http: Any = None,
        now: Any = None,
    ) -> str:
        captured.update({"provider": provider, "client_id": client_id})
        return "fresh-token"

    class FakeWebmasterClient:
        def __init__(self, *, token: str) -> None:
            captured["token"] = token

    monkeypatch.setattr(_services_module, "ensure_fresh_access_token", fake_ensure)
    monkeypatch.setattr(_services_module, "YandexWebmasterClient", FakeWebmasterClient)
    service = SeoService(
        _config(),
        secrets=MemoryStore({"yandex_webmaster_token": "stale"}),  # type: ignore[arg-type]
    )

    service._build_client("yandex_webmaster")

    assert captured == {
        "provider": "yandex",
        "client_id": CLIENT_ID,
        "token": "fresh-token",
    }


def test_service_uses_the_refreshed_token_for_metrika(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_ensure(
        store: Any,
        provider: str,
        *,
        client_id: str = "",
        http: Any = None,
        now: Any = None,
    ) -> str:
        captured.update({"provider": provider, "client_id": client_id})
        return "fresh-token"

    class FakeMetrikaClient:
        def __init__(self, *, token: str) -> None:
            captured["token"] = token

    monkeypatch.setattr(_services_module, "ensure_fresh_access_token", fake_ensure)
    monkeypatch.setattr(_services_module, "YandexMetrikaClient", FakeMetrikaClient)
    service = SeoService(
        _config(),
        secrets=MemoryStore({"yandex_metrika_token": "stale"}),  # type: ignore[arg-type]
    )

    service._build_client("yandex_metrika")

    assert captured == {
        "provider": "metrika",
        "client_id": CLIENT_ID,
        "token": "fresh-token",
    }


def test_service_refresh_failure_is_reported_as_auth_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_ensure(*_args: Any, **_kwargs: Any) -> str:
        raise SeoError(
            code=AUTH_INVALID,
            message="Yandex Webmaster OAuth refresh failed; authorize again",
            next_action="lv24-seo auth login --provider yandex",
        )

    monkeypatch.setattr(_services_module, "ensure_fresh_access_token", failing_ensure)
    service = SeoService(
        _config(),
        secrets=MemoryStore({"yandex_webmaster_token": "expired"}),  # type: ignore[arg-type]
    )

    with pytest.raises(SeoError) as excinfo:
        service._build_client("yandex_webmaster")

    assert excinfo.value.code == AUTH_INVALID
    assert "auth login --provider yandex" in (excinfo.value.next_action or "")


def test_service_static_token_still_builds_a_client() -> None:
    service = SeoService(
        _config(),
        secrets=MemoryStore({"yandex_webmaster_token": "static"}),  # type: ignore[arg-type]
    )

    client = service._build_client("yandex_webmaster")

    assert client is not None


def test_service_properties_treat_a_refresh_token_as_a_credential() -> None:
    service = SeoService(
        _config(),
        secrets=MemoryStore(  # type: ignore[arg-type]
            {
                "yandex_refresh_token": "refresh-only",
                "yandex_oauth_client_secret": CLIENT_SECRET,
            }
        ),
    )

    payload = service.properties()
    entries = {entry["provider"]: entry for entry in payload["data"]["providers"]}

    assert entries["yandex_webmaster"]["token_present"] is True
    assert entries["yandex_webmaster"]["token_source"] == "refresh_token"
    assert entries["yandex_metrika"]["token_present"] is False
