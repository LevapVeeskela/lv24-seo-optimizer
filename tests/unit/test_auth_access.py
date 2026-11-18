"""Unit tests for the provider data-access summary in `auth status`."""

from __future__ import annotations

from lv24_seo.cli.auth import _access_summary
from lv24_seo.core.config import GscConfig, ProvidersConfig, SeoConfig
from lv24_seo.core.secrets import SecretStatus


def _statuses(*names: str) -> dict[str, SecretStatus]:
    return {name: SecretStatus(name=name, present=True, source="keyring") for name in names}


def test_no_credentials_means_no_provider_is_ready() -> None:
    access = _access_summary(SeoConfig(), {})

    assert {provider: entry["mode"] for provider, entry in access.items()} == {
        "gsc": "none",
        "yandex": "none",
        "metrika": "none",
        "bing": "none",
        "crux": "none",
        "psi": "none",
    }
    assert all(entry["ready"] is False for entry in access.values())


def test_yandex_and_metrika_tokens_are_data_access_without_oauth() -> None:
    access = _access_summary(
        SeoConfig(),
        _statuses("yandex_webmaster_token", "yandex_metrika_token"),
    )

    assert access["yandex"] == {"mode": "token", "ready": True}
    assert access["metrika"] == {"mode": "token", "ready": True}


def test_gsc_service_account_wins_over_tokens() -> None:
    config = SeoConfig(
        providers=ProvidersConfig(
            gsc=GscConfig(service_account_file="C:/0project/lv24/seo/gsc-sa.json")
        )
    )

    access = _access_summary(config, _statuses("gsc_token", "gsc_refresh_token"))

    assert access["gsc"] == {"mode": "service_account", "ready": True}


def test_gsc_token_and_oauth_fallbacks() -> None:
    token = _access_summary(SeoConfig(), _statuses("gsc_token"))
    oauth = _access_summary(SeoConfig(), _statuses("gsc_refresh_token"))

    assert token["gsc"]["mode"] == "token"
    assert oauth["gsc"]["mode"] == "oauth"


def test_metrika_can_use_the_shared_yandex_oauth_refresh_token() -> None:
    access = _access_summary(SeoConfig(), _statuses("yandex_refresh_token"))

    assert access["yandex"]["mode"] == "oauth"
    assert access["metrika"]["mode"] == "oauth"


def test_bing_crux_and_psi_keys_are_reported() -> None:
    access = _access_summary(
        SeoConfig(),
        _statuses("bing_api_key", "crux_api_key", "psi_api_key"),
    )

    assert access["bing"] == {"mode": "token", "ready": True}
    assert access["crux"] == {"mode": "token", "ready": True}
    assert access["psi"] == {"mode": "token", "ready": True}


def test_blank_service_account_path_is_not_configured() -> None:
    config = SeoConfig(providers=ProvidersConfig(gsc=GscConfig(service_account_file="   ")))

    access = _access_summary(config, {})

    assert access["gsc"]["mode"] == "none"
