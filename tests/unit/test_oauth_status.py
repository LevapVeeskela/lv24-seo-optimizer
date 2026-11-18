"""Offline tests for OAuth readiness diagnostics, scope persistence and redirect checks.

Every token here is synthetic. No Google or Yandex endpoint is contacted: the one refresh
test uses ``httpx.MockTransport`` and the CLI tests only read the local secret store.
"""

from __future__ import annotations

import json
import logging
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from lv24_seo.cli import auth_google, auth_yandex
from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import (
    AuthConfig,
    GscConfig,
    OAuthClientConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
    YandexMetrikaConfig,
    YandexWebmasterConfig,
)
from lv24_seo.core.oauth import TokenResponse
from lv24_seo.core.secrets import OAUTH_SECRET_NAMES, SECRET_NAMES, SecretStore
from lv24_seo.providers.gsc.auth import WEBMASTERS_READONLY_SCOPE
from lv24_seo.providers.gsc.oauth import (
    ACCESS_TOKEN_SECRET,
    EXPIRES_AT_SECRET,
    REFRESH_TOKEN_SECRET,
    SCOPE_SECRET,
    OAuthTokenProvider,
)
from lv24_seo.providers.gsc.oauth import (
    CLIENT_SECRET_NAME as GSC_CLIENT_SECRET,
)
from lv24_seo.providers.oauth_status import (
    OAUTH_CLIENT_MISSING,
    OAUTH_CLIENT_SECRET_MISSING,
    OAUTH_PROVIDERS,
    OAUTH_READY,
    OAUTH_REDIRECT_INVALID,
    OAUTH_SCOPE_MISSING,
    OAUTH_SCOPE_UNKNOWN,
    OAUTH_TOKEN_EXPIRED,
    OAUTH_TOKEN_MISSING,
    oauth_readiness,
    oauth_readiness_all,
    parse_recorded_scopes,
)
from lv24_seo.providers.yandex_oauth import (
    OAuthTokens,
    provider_scope_name,
    provider_secret_names,
    store_tokens,
)

RUNNER = CliRunner()
NOW = datetime.now(UTC).replace(microsecond=0)
GSC_CLIENT_ID = "1234567890-synthetic.apps.googleusercontent.com"
YANDEX_CLIENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CLIENT_SECRET = "synthetic-client-secret"
ACCESS = "synthetic-access-token"
REFRESH = "synthetic-refresh-token"
GSC_SCOPES = (WEBMASTERS_READONLY_SCOPE,)
YANDEX_SCOPES = ("webmaster:hostinfo", "metrika:read")

# -- helpers ---------------------------------------------------------------------------------


def _store(tmp_path: Path) -> SecretStore:
    return SecretStore(env={}, data_dir=tmp_path / "data", allow_file=True)


def _config(
    tmp_path: Path,
    *,
    gsc_client_id: str = GSC_CLIENT_ID,
    yandex_client_id: str = YANDEX_CLIENT_ID,
    gsc_redirect: str = "",
    yandex_redirect: str = "",
    gsc_scopes: list[str] | None = None,
    yandex_scopes: list[str] | None = None,
) -> SeoConfig:
    return SeoConfig(
        providers=ProvidersConfig(
            gsc=GscConfig(property="sc-domain:example.com"),
            yandex_webmaster=YandexWebmasterConfig(host_id="https:example.com:443"),
            yandex_metrika=YandexMetrikaConfig(counter="123456"),
        ),
        auth=AuthConfig(
            gsc=OAuthClientConfig(
                client_id=gsc_client_id,
                scopes=list(gsc_scopes) if gsc_scopes is not None else list(GSC_SCOPES),
                redirect_uri=gsc_redirect,
            ),
            yandex=OAuthClientConfig(
                client_id=yandex_client_id,
                scopes=list(yandex_scopes) if yandex_scopes is not None else list(YANDEX_SCOPES),
                redirect_uri=yandex_redirect,
            ),
        ),
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


def _seed_ready(
    store: SecretStore,
    provider: str,
    *,
    expiry: datetime | None = NOW + timedelta(hours=1),
    with_refresh: bool = True,
    scopes: str | None = None,
) -> None:
    if provider == "gsc":
        store.set(GSC_CLIENT_SECRET, CLIENT_SECRET)
        store.set(ACCESS_TOKEN_SECRET, ACCESS)
        if with_refresh:
            store.set(REFRESH_TOKEN_SECRET, REFRESH)
        if expiry is not None:
            store.set(EXPIRES_AT_SECRET, expiry.isoformat(timespec="seconds"))
        store.set(SCOPE_SECRET, scopes if scopes is not None else " ".join(GSC_SCOPES))
        return
    access_name, refresh_name, expiry_name = provider_secret_names(provider)
    store.set("yandex_oauth_client_secret", CLIENT_SECRET)
    store.set(access_name, ACCESS)
    if with_refresh:
        store.set(refresh_name, REFRESH)
    if expiry is not None:
        store.set(expiry_name, expiry.isoformat(timespec="seconds"))
    store.set(
        provider_scope_name(provider),
        scopes if scopes is not None else " ".join(YANDEX_SCOPES),
    )


def _cli_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "LV24_SEO_DATA_DIR": str(tmp_path / "data"),
        "LV24_SEO_ALLOW_FILE_SECRETS": "1",
        # Blank means unset: the developer machine must not decide these assertions.
        "LV24_SEO_GSC_TOKEN": "",
        "LV24_SEO_GSC_ACCESS_TOKEN": "",
        "LV24_SEO_GSC_REFRESH_TOKEN": "",
        "LV24_SEO_GSC_OAUTH_CLIENT_SECRET": "",
        "LV24_SEO_GSC_TOKEN_SCOPES": "",
        "LV24_SEO_YANDEX_WEBMASTER_TOKEN": "",
        "LV24_SEO_YANDEX_REFRESH_TOKEN": "",
        "LV24_SEO_YANDEX_METRIKA_TOKEN": "",
        "LV24_SEO_YANDEX_METRIKA_REFRESH_TOKEN": "",
        "LV24_SEO_YANDEX_OAUTH_CLIENT_SECRET": "",
        "LV24_SEO_YANDEX_TOKEN_SCOPES": "",
        "LV24_SEO_YANDEX_METRIKA_TOKEN_SCOPES": "",
    }
    env.update(overrides)
    return env


def _write_config(
    tmp_path: Path,
    *,
    gsc_client_id: str = GSC_CLIENT_ID,
    yandex_client_id: str = YANDEX_CLIENT_ID,
    gsc_redirect: str = "",
    yandex_redirect: str = "",
) -> Path:
    path = tmp_path / "seo.toml"
    lines = [
        "[storage]",
        'data_dir = "./data"',
        "",
        "[auth.gsc]",
        f'client_id = "{gsc_client_id}"',
        f'redirect_uri = "{gsc_redirect}"',
        "",
        "[auth.yandex]",
        f'client_id = "{yandex_client_id}"',
        f'redirect_uri = "{yandex_redirect}"',
    ]
    (tmp_path / "data").mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _invoke(tmp_path: Path, args: list[str], **env: str) -> Any:
    config_path = tmp_path / "seo.toml"
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    auth = document.get("auth") or {}
    gsc = auth.get("gsc") or {}
    yandex = auth.get("yandex") or {}
    # The environment wins over seo.toml; pin it to the file so a developer shell cannot
    # change the scenario under test.
    env.setdefault("LV24_SEO_GSC_CLIENT_ID", str(gsc.get("client_id", "")))
    env.setdefault("LV24_SEO_GSC_REDIRECT_URI", str(gsc.get("redirect_uri", "")))
    env.setdefault("LV24_SEO_YANDEX_CLIENT_ID", str(yandex.get("client_id", "")))
    env.setdefault("LV24_SEO_YANDEX_REDIRECT_URI", str(yandex.get("redirect_uri", "")))
    return RUNNER.invoke(
        cli_main.app,
        [*args, "--config", str(config_path)],
        env=_cli_env(tmp_path, **env),
    )


def _json(stdout: str) -> dict[str, Any]:
    payload: Any = json.loads(stdout.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


# -- secret-name contract --------------------------------------------------------------------


def test_scope_secret_names_are_oauth_only_and_secret_names_stay_stable() -> None:
    assert SECRET_NAMES == (
        "gsc_token",
        "yandex_webmaster_token",
        "yandex_metrika_token",
        "bing_api_key",
        "crux_api_key",
        "psi_api_key",
        "smtp_password",
        "telegram_bot_token",
    )
    for name in ("gsc_token_scopes", "yandex_token_scopes", "yandex_metrika_token_scopes"):
        assert name in OAUTH_SECRET_NAMES
        assert name not in SECRET_NAMES


def test_parse_recorded_scopes_accepts_space_and_json_forms() -> None:
    assert parse_recorded_scopes("webmaster:hostinfo metrika:read") == YANDEX_SCOPES
    assert parse_recorded_scopes('["webmaster:hostinfo", "metrika:read"]') == YANDEX_SCOPES
    assert parse_recorded_scopes("") == ()
    assert parse_recorded_scopes("[not json") == ()


# -- the eight states ------------------------------------------------------------------------


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_client_missing(tmp_path: Path, provider: str) -> None:
    config = _config(tmp_path, gsc_client_id="", yandex_client_id="")
    readiness = oauth_readiness(config, _store(tmp_path), provider, now=NOW)

    assert readiness.state == OAUTH_CLIENT_MISSING
    assert readiness.ok is False
    assert readiness.client_id_present is False
    link = "console.cloud.google.com" if provider == "gsc" else "oauth.yandex.ru"
    assert link in readiness.detail
    assert readiness.next_action


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_placeholder_client_id_is_not_ready(tmp_path: Path, provider: str) -> None:
    config = _config(
        tmp_path,
        gsc_client_id="test-client-id",
        yandex_client_id="yandex-client-id",
    )
    readiness = oauth_readiness(config, _store(tmp_path), provider, now=NOW)

    assert readiness.state == OAUTH_CLIENT_MISSING
    assert "placeholder" in readiness.detail
    assert readiness.client_id_present is True


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_client_secret_missing(tmp_path: Path, provider: str) -> None:
    readiness = oauth_readiness(_config(tmp_path), _store(tmp_path), provider, now=NOW)

    assert readiness.state == OAUTH_CLIENT_SECRET_MISSING
    assert readiness.client_id_present is True
    assert readiness.client_secret_present is False
    expected = "gsc_oauth_secret" if provider == "gsc" else "yandex_oauth_secret"
    assert expected in readiness.next_action


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_token_missing(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    if provider == "gsc":
        store.set(GSC_CLIENT_SECRET, CLIENT_SECRET)
    else:
        store.set("yandex_oauth_client_secret", CLIENT_SECRET)
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_TOKEN_MISSING
    assert readiness.access_token_present is False
    assert readiness.refresh_token_present is False
    assert f"auth login --provider {provider}" in readiness.next_action


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_token_expired_with_refresh_says_refresh_available(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    _seed_ready(store, provider, expiry=NOW - timedelta(minutes=5))
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_TOKEN_EXPIRED
    assert readiness.refresh_token_present is True
    assert "refresh available" in readiness.detail
    assert "refreshes automatically" in readiness.next_action


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_token_expired_without_refresh_requires_login(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    _seed_ready(store, provider, expiry=NOW - timedelta(minutes=5), with_refresh=False)
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_TOKEN_EXPIRED
    assert readiness.refresh_token_present is False
    assert "no refresh token" in readiness.detail
    assert f"auth login --provider {provider}" in readiness.next_action
    assert any("cannot be renewed" in item for item in readiness.limitations)


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_scope_missing(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    if provider == "gsc":
        _seed_ready(store, provider, scopes="https://www.googleapis.com/auth/analytics.readonly")
        missing = WEBMASTERS_READONLY_SCOPE
    else:
        _seed_ready(store, provider, scopes="webmaster:hostinfo")
        missing = "metrika:read"
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_SCOPE_MISSING
    assert readiness.scopes_recorded is True
    assert missing in readiness.detail
    assert f"auth login --provider {provider}" in readiness.next_action


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_scope_unknown_is_a_limitation_not_a_pass(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    _seed_ready(store, provider)
    if provider == "gsc":
        store.delete(SCOPE_SECRET)
    else:
        store.delete(provider_scope_name(provider))
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_SCOPE_UNKNOWN
    assert readiness.ok is False
    assert readiness.scopes_recorded is False
    assert any("unknown, not passed" in item for item in readiness.limitations)


@pytest.mark.parametrize(
    ("provider", "redirect", "valid"),
    [
        ("gsc", "", True),
        ("gsc", "http://127.0.0.1:8123/callback", True),
        ("gsc", "http://127.0.0.1:1/callback", True),
        ("gsc", "http://localhost:8123/callback", False),
        ("gsc", "https://127.0.0.1:8123/callback", False),
        ("gsc", "http://127.0.0.1/callback", False),
        ("gsc", "http://127.0.0.1:8123/callback?x=1", False),
        ("gsc", "http://example.com:8123/callback", False),
        ("gsc", "not a url", False),
        ("yandex", "", True),
        ("yandex", "https://oauth.yandex.ru/verification_code", True),
        ("yandex", "https://oauth.yandex.ru/verification_code/", False),
        ("yandex", "https://example.com/verification_code", False),
        ("yandex", "http://oauth.yandex.ru/verification_code", False),
        ("metrika", "https://oauth.yandex.ru/verification_code", True),
        ("metrika", "http://127.0.0.1:8123/callback", False),
    ],
)
def test_redirect_validation(tmp_path: Path, provider: str, redirect: str, valid: bool) -> None:
    store = _store(tmp_path)
    _seed_ready(store, provider)
    config = _config(
        tmp_path,
        gsc_redirect=redirect if provider == "gsc" else "",
        yandex_redirect=redirect if provider != "gsc" else "",
    )
    readiness = oauth_readiness(config, store, provider, now=NOW)

    if valid:
        assert readiness.state == OAUTH_READY
    else:
        assert readiness.state == OAUTH_REDIRECT_INVALID
        assert "redirect_uri" in readiness.detail


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
def test_ready_state(tmp_path: Path, provider: str) -> None:
    store = _store(tmp_path)
    _seed_ready(store, provider)
    readiness = oauth_readiness(_config(tmp_path), store, provider, now=NOW)

    assert readiness.state == OAUTH_READY
    assert readiness.ok is True
    assert readiness.next_action == ""
    assert readiness.limitations == ()


def test_oauth_readiness_all_covers_every_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for provider in OAUTH_PROVIDERS:
        _seed_ready(store, provider)

    readiness = oauth_readiness_all(_config(tmp_path), store, now=NOW)

    assert set(readiness) == set(OAUTH_PROVIDERS)
    assert all(item.state == OAUTH_READY for item in readiness.values())


# -- scope persistence -----------------------------------------------------------------------


def test_gsc_login_persists_requested_scopes_when_the_response_omits_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LV24_SEO_ALLOW_FILE_SECRETS", "1")
    store = _store(tmp_path)
    store.set(GSC_CLIENT_SECRET, CLIENT_SECRET)
    monkeypatch.setattr(
        auth_google,
        "exchange_code",
        lambda *args, **kwargs: TokenResponse(
            access_token=ACCESS, refresh_token=REFRESH, expires_in=3600
        ),
    )

    auth_google.login_gsc(_config(tmp_path), code="the-code", port=8123)

    assert store.get(SCOPE_SECRET) == " ".join(GSC_SCOPES)


def test_gsc_login_persists_the_scope_the_provider_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LV24_SEO_ALLOW_FILE_SECRETS", "1")
    store = _store(tmp_path)
    store.set(GSC_CLIENT_SECRET, CLIENT_SECRET)
    monkeypatch.setattr(
        auth_google,
        "exchange_code",
        lambda *args, **kwargs: TokenResponse(
            access_token=ACCESS, refresh_token=REFRESH, expires_in=3600, scope=GSC_SCOPES[0]
        ),
    )

    auth_google.login_gsc(_config(tmp_path), code="the-code", port=8123)

    assert store.get(SCOPE_SECRET) == GSC_SCOPES[0]


def test_gsc_refresh_records_the_returned_scopes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_ready(store, "gsc", expiry=NOW - timedelta(minutes=5))
    store.delete(SCOPE_SECRET)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "expires_in": 3600,
                "scope": GSC_SCOPES[0],
            },
        )

    provider = OAuthTokenProvider(
        store=store,
        client_id=GSC_CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW.timestamp(),
    )

    assert provider.access_token() == "new-access-token"
    assert len(calls) == 1
    assert store.get(SCOPE_SECRET) == GSC_SCOPES[0]


def test_yandex_store_tokens_persists_scopes_and_refresh_keeps_them(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store_tokens(
        store,
        "yandex",
        OAuthTokens(access_token=ACCESS, scope=YANDEX_SCOPES),
    )
    assert store.get(provider_scope_name("yandex")) == " ".join(YANDEX_SCOPES)

    store_tokens(store, "yandex", OAuthTokens(access_token="newer"))
    assert store.get(provider_scope_name("yandex")) == " ".join(YANDEX_SCOPES)

    store_tokens(
        store,
        "metrika",
        OAuthTokens(access_token=ACCESS),
        keep_refresh=False,
        requested_scopes=("metrika:read",),
    )
    assert store.get(provider_scope_name("metrika")) == "metrika:read"


def test_logout_deletes_the_recorded_scopes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_ready(store, "gsc")
    _seed_ready(store, "yandex")

    auth_google.logout_gsc(store)
    auth_yandex.logout_yandex(store, "yandex")

    assert store.get(SCOPE_SECRET) is None
    assert store.get(provider_scope_name("yandex")) is None
    assert store.get(GSC_CLIENT_SECRET) == CLIENT_SECRET
    assert store.get("yandex_oauth_client_secret") == CLIENT_SECRET


# -- CLI surfaces ----------------------------------------------------------------------------


def test_auth_status_keeps_the_provider_rows_and_adds_oauth(tmp_path: Path) -> None:
    _write_config(tmp_path)
    store = _store(tmp_path)
    _seed_ready(store, "gsc")

    result = _invoke(tmp_path, ["auth", "status", "--json"])

    assert result.exit_code == 0, result.stdout
    data = _json(result.stdout)["data"]
    assert {"providers", "present", "missing", "oauth"} <= set(data)
    for row in data["providers"]:
        assert {"provider", "secret", "present", "source", "expires_at"} <= set(row)
    assert set(data["oauth"]) == set(OAUTH_PROVIDERS)
    gsc = data["oauth"]["gsc"]
    assert {
        "state",
        "client_id_present",
        "client_secret_present",
        "access_token_present",
        "refresh_token_present",
        "expires_at",
        "scopes_recorded",
        "next_action",
        "limitations",
    } <= set(gsc)
    assert gsc["state"] == OAUTH_READY
    assert gsc["client_id_present"] is True
    assert gsc["client_secret_present"] is True
    assert gsc["access_token_present"] is True
    assert gsc["refresh_token_present"] is True
    assert gsc["scopes_recorded"] is True
    assert isinstance(gsc["limitations"], list)
    assert data["oauth"]["yandex"]["state"] == OAUTH_CLIENT_SECRET_MISSING
    assert data["oauth"]["metrika"]["state"] == OAUTH_CLIENT_SECRET_MISSING


def test_auth_status_text_shows_the_code_and_next_action(tmp_path: Path) -> None:
    _write_config(tmp_path, yandex_client_id="")

    result = _invoke(tmp_path, ["auth", "status"])

    assert result.exit_code == 0, result.stdout
    assert "oauth readiness:" in result.stdout
    assert OAUTH_CLIENT_MISSING in result.stdout
    assert "oauth.yandex.ru" in result.stdout


def test_doctor_reports_one_check_per_oauth_provider_with_codes(tmp_path: Path) -> None:
    _write_config(tmp_path)
    store = _store(tmp_path)
    store.set(GSC_CLIENT_SECRET, CLIENT_SECRET)

    result = _invoke(tmp_path, ["doctor", "--json"])

    assert result.exit_code == 0, result.stdout
    checks = {check["id"]: check for check in _json(result.stdout)["data"]["checks"]}
    for provider in OAUTH_PROVIDERS:
        check = checks[f"{provider}_oauth"]
        assert {"id", "ok", "detail", "code", "next_action"} <= set(check)
        assert isinstance(check["ok"], bool)
        assert check["code"]
        assert isinstance(check["next_action"], str)
    assert checks["gsc_oauth"]["code"] == OAUTH_TOKEN_MISSING
    assert checks["yandex_oauth"]["code"] == OAUTH_CLIENT_SECRET_MISSING
    assert checks["metrika_oauth"]["code"] == OAUTH_CLIENT_SECRET_MISSING
    assert checks["gsc_oauth"]["ok"] is False
    assert (
        "lv24-seo auth set --provider yandex_oauth_secret --stdin"
        in (checks["yandex_oauth"]["next_action"])
    )


def test_doctor_reports_the_registration_link_for_a_missing_app(tmp_path: Path) -> None:
    _write_config(tmp_path, gsc_client_id="", yandex_client_id="")

    result = _invoke(tmp_path, ["doctor", "--json"])

    assert result.exit_code == 0, result.stdout
    checks = {check["id"]: check for check in _json(result.stdout)["data"]["checks"]}
    assert checks["gsc_oauth"]["code"] == OAUTH_CLIENT_MISSING
    assert "console.cloud.google.com" in checks["gsc_oauth"]["detail"]
    assert checks["yandex_oauth"]["code"] == OAUTH_CLIENT_MISSING
    assert "oauth.yandex.ru" in checks["yandex_oauth"]["detail"]
    assert checks["metrika_oauth"]["code"] == OAUTH_CLIENT_MISSING


def test_doctor_does_not_make_oauth_fatal(tmp_path: Path) -> None:
    _write_config(tmp_path, gsc_client_id="", yandex_client_id="")

    result = _invoke(tmp_path, ["doctor", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = _json(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["healthy"] is True


@pytest.mark.parametrize("surface", ["auth-status", "doctor"])
def test_token_values_never_reach_the_output(
    tmp_path: Path, surface: str, caplog: pytest.LogCaptureFixture
) -> None:
    _write_config(tmp_path)
    store = _store(tmp_path)
    for provider in OAUTH_PROVIDERS:
        _seed_ready(store, provider)

    args = ["auth", "status", "--json"] if surface == "auth-status" else ["doctor", "--json"]
    with caplog.at_level(logging.DEBUG):
        result = _invoke(tmp_path, args)
    output = result.stdout + result.stderr

    assert result.exit_code == 0, result.stdout
    for value in (ACCESS, REFRESH, CLIENT_SECRET):
        assert value not in output
        assert all(value not in record.getMessage() for record in caplog.records)
    assert "synthetic-access" not in output


def test_readiness_repr_and_dict_carry_no_token_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_ready(store, "gsc")

    readiness = oauth_readiness(_config(tmp_path), store, "gsc", now=NOW)
    rendered = repr(readiness) + json.dumps(readiness.as_dict())

    for value in (ACCESS, REFRESH, CLIENT_SECRET):
        assert value not in rendered
