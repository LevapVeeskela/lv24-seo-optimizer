"""Contract tests for the layered configuration loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import platformdirs
import pytest
from pydantic import Field

from lv24_seo.core.config import (
    SeoConfig,
    load_config,
    redacted_dump,
    resolve_config_path,
)
from lv24_seo.core.errors import CONFIG_INVALID, SeoError


@pytest.fixture(autouse=True)
def user_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test away from the real ``%APPDATA%`` / ``~/.config`` file."""
    root = tmp_path / "user-config"
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda *args, **kwargs: str(root))
    return root


def _write_config(directory: Path, body: str) -> Path:
    path = directory / "seo.toml"
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


def _write_user_config(root: Path, body: str) -> Path:
    path = root / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return path


def test_defaults_without_any_file(tmp_path: Path) -> None:
    cfg = load_config(cwd=tmp_path)

    assert cfg.project.name == ""
    assert cfg.project.base_url == ""
    assert cfg.project.default_property == ""
    assert cfg.providers.gsc is None
    assert cfg.providers.yandex_webmaster is None
    assert cfg.providers.yandex_metrika is None
    assert cfg.providers.bing is None
    assert cfg.providers.crux is None
    assert cfg.providers.psi is None

    assert cfg.report.locale == "ru"
    assert cfg.report.timezone == "Europe/Minsk"
    assert cfg.report.week_start == "monday"
    assert cfg.report.thresholds.clicks_pct == 10.0
    assert cfg.report.thresholds.impressions_pct == 15.0
    assert cfg.report.thresholds.position_abs == 1.5
    assert cfg.report.email.enabled is False
    assert cfg.report.email.to == []
    assert cfg.report.email.email_from == ""
    assert cfg.report.email.only_on_change is True
    assert cfg.report.email.smtp_port == 587
    assert cfg.report.telegram.enabled is False
    assert cfg.report.telegram.chat_id == ""
    assert cfg.report.telegram.severity == "critical"

    assert cfg.crawl.max_urls == 500
    assert cfg.crawl.concurrency == 4
    assert cfg.crawl.respect_robots is True
    assert cfg.crawl.user_agent.startswith("lv24-seo/")

    assert cfg.storage.data_dir == Path("data")
    assert cfg.storage.raw_retention_days == 90


def test_resolve_explicit_path(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[project]\nname = "explicit"\n')
    assert resolve_config_path(path) == path


def test_resolve_explicit_path_beats_cwd(tmp_path: Path) -> None:
    _write_config(tmp_path, '[project]\nname = "local"\n')
    explicit = tmp_path / "custom.toml"
    explicit.write_text('[project]\nname = "explicit"\n', encoding="utf-8")

    assert resolve_config_path(explicit, cwd=tmp_path) == explicit


def test_resolve_prefers_cwd_over_user_config(tmp_path: Path, user_config_dir: Path) -> None:
    local = _write_config(tmp_path, '[project]\nname = "local"\n')
    _write_user_config(user_config_dir, '[project]\nname = "user"\n')

    assert resolve_config_path(cwd=tmp_path) == local


def test_resolve_falls_back_to_user_config(tmp_path: Path, user_config_dir: Path) -> None:
    user_config = _write_user_config(user_config_dir, '[project]\nname = "user"\n')

    assert resolve_config_path(cwd=tmp_path) == user_config
    assert load_config(cwd=tmp_path).project.name == "user"


def test_resolve_returns_none_when_no_file(tmp_path: Path) -> None:
    assert resolve_config_path(cwd=tmp_path) is None


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(SeoError) as resolve_error:
        resolve_config_path(missing)
    assert resolve_error.value.code == CONFIG_INVALID
    assert resolve_error.value.next_action

    with pytest.raises(SeoError) as load_error:
        load_config(missing, cwd=tmp_path)
    assert load_error.value.code == CONFIG_INVALID
    assert str(missing) in load_error.value.message


def test_values_from_toml(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
        [project]
        name = "example.com"
        base_url = "https://example.com"
        default_property = "sc-domain:example.com"

        [providers.gsc]
        property = "sc-domain:example.com"
        auth = "keychain"

        [providers.yandex_webmaster]
        host_id = "https:example.com:443"

        [providers.yandex_metrika]
        counter = 123456
        goals = [1, 2]

        [providers.bing]
        site = "https://example.com"

        [providers.crux]
        origin = "https://example.com"

        [providers.psi]
        strategy = "desktop"
        urls = ["https://example.com/"]

        [report]
        locale = "en"
        timezone = "UTC"
        week_start = "sunday"
        thresholds = { clicks_pct = 5, impressions_pct = 7, position_abs = 2.0 }

        [report.email]
        enabled = true
        to = ["a@example.com", "b@example.com"]
        from = "seo@example.com"
        only_on_change = false
        smtp_host = "smtp.example.com"
        smtp_port = 465
        smtp_user = "seo-bot"

        [report.telegram]
        enabled = true
        chat_id = "123456789"
        severity = "all"

        [crawl]
        max_urls = 250
        concurrency = 2
        respect_robots = false
        user_agent = "test-agent"

        [storage]
        data_dir = "./var/seo"
        raw_retention_days = 30
        """,
    )

    cfg = load_config(path, cwd=tmp_path)

    assert cfg.project.name == "example.com"
    assert cfg.project.base_url == "https://example.com"
    assert cfg.project.default_property == "sc-domain:example.com"

    assert cfg.providers.gsc is not None
    assert cfg.providers.gsc.property == "sc-domain:example.com"
    assert cfg.providers.gsc.auth == "keychain"
    assert cfg.providers.yandex_webmaster is not None
    assert cfg.providers.yandex_webmaster.host_id == "https:example.com:443"
    assert cfg.providers.yandex_metrika is not None
    assert cfg.providers.yandex_metrika.counter == "123456"
    assert cfg.providers.yandex_metrika.goals == [1, 2]
    assert cfg.providers.bing is not None
    assert cfg.providers.bing.site == "https://example.com"
    assert cfg.providers.crux is not None
    assert cfg.providers.crux.origin == "https://example.com"
    assert cfg.providers.psi is not None
    assert cfg.providers.psi.strategy == "desktop"
    assert cfg.providers.psi.urls == ["https://example.com/"]

    assert cfg.report.locale == "en"
    assert cfg.report.timezone == "UTC"
    assert cfg.report.week_start == "sunday"
    assert cfg.report.thresholds.clicks_pct == 5.0
    assert cfg.report.thresholds.impressions_pct == 7.0
    assert cfg.report.thresholds.position_abs == 2.0
    assert cfg.report.email.enabled is True
    assert cfg.report.email.to == ["a@example.com", "b@example.com"]
    assert cfg.report.email.email_from == "seo@example.com"
    assert cfg.report.email.only_on_change is False
    assert cfg.report.email.smtp_host == "smtp.example.com"
    assert cfg.report.email.smtp_port == 465
    assert cfg.report.email.smtp_user == "seo-bot"
    assert cfg.report.telegram.enabled is True
    assert cfg.report.telegram.chat_id == "123456789"
    assert cfg.report.telegram.severity == "all"

    assert cfg.crawl.max_urls == 250
    assert cfg.crawl.concurrency == 2
    assert cfg.crawl.respect_robots is False
    assert cfg.crawl.user_agent == "test-agent"

    assert cfg.storage.data_dir == Path("./var/seo")
    assert cfg.storage.raw_retention_days == 30


@pytest.mark.parametrize("locale", ["ru", "en", "zh"])
def test_report_locale_supports_ru_en_zh(tmp_path: Path, locale: str) -> None:
    path = _write_config(tmp_path, f'[report]\nlocale = "{locale}"\n')

    assert load_config(path, cwd=tmp_path).report.locale == locale


def test_report_locale_rejects_unsupported_value(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[report]\nlocale = "de"\n')

    with pytest.raises(SeoError) as exc:
        load_config(path, cwd=tmp_path)

    assert exc.value.code == CONFIG_INVALID
    assert "report.locale" in exc.value.message
    assert exc.value.next_action


def test_explicit_file_wins_over_cwd_file(tmp_path: Path) -> None:
    _write_config(tmp_path, '[project]\nname = "local"\n')
    explicit = tmp_path / "custom.toml"
    explicit.write_text('[project]\nname = "explicit"\n', encoding="utf-8")

    assert load_config(explicit, cwd=tmp_path).project.name == "explicit"


def test_env_overrides_file(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
        [project]
        name = "example.com"

        [providers.gsc]
        property = "sc-domain:file.example"
        auth = "env"

        [report]
        locale = "ru"
        timezone = "UTC"

        [storage]
        data_dir = "./file-data"
        """,
    )
    env = {
        "LV24_SEO_GSC_PROPERTY": "sc-domain:env.example",
        "LV24_SEO_YANDEX_HOST_ID": "https:env.example:443",
        "LV24_SEO_YANDEX_METRIKA_COUNTER": "987654",
        "LV24_SEO_REPORT_LOCALE": "en",
        "LV24_SEO_REPORT_TIMEZONE": "Europe/Minsk",
        "LV24_SEO_DATA_DIR": "./env-data",
        "LV24_SEO_MAIL_TO": "a@example.com, b@example.com",
        "LV24_SEO_SMTP_HOST": "smtp.env.example",
        "LV24_SEO_SMTP_PORT": "2525",
        "LV24_SEO_SMTP_USER": "env-user",
        "LV24_SEO_SMTP_FROM": "seo@env.example",
        "LV24_SEO_TELEGRAM_CHAT_ID": "555",
    }

    cfg = load_config(path, env=env, cwd=tmp_path)

    assert cfg.project.name == "example.com"
    assert cfg.providers.gsc is not None
    assert cfg.providers.gsc.property == "sc-domain:env.example"
    assert cfg.providers.gsc.auth == "env"
    assert cfg.providers.yandex_webmaster is not None
    assert cfg.providers.yandex_webmaster.host_id == "https:env.example:443"
    assert cfg.providers.yandex_metrika is not None
    assert cfg.providers.yandex_metrika.counter == "987654"
    assert cfg.report.locale == "en"
    assert cfg.report.timezone == "Europe/Minsk"
    assert cfg.storage.data_dir == Path("./env-data")
    assert cfg.report.email.to == ["a@example.com", "b@example.com"]
    assert cfg.report.email.smtp_host == "smtp.env.example"
    assert cfg.report.email.smtp_port == 2525
    assert cfg.report.email.smtp_user == "env-user"
    assert cfg.report.email.email_from == "seo@env.example"
    assert cfg.report.telegram.chat_id == "555"
    assert cfg.report.telegram.enabled is False


def test_env_mail_to_parses_comma_list(tmp_path: Path) -> None:
    cfg = load_config(
        cwd=tmp_path,
        env={"LV24_SEO_MAIL_TO": " a@example.com, b@example.com ,, "},
    )
    assert cfg.report.email.to == ["a@example.com", "b@example.com"]


def test_env_smtp_port_parses_int(tmp_path: Path) -> None:
    cfg = load_config(cwd=tmp_path, env={"LV24_SEO_SMTP_PORT": "2525"})
    assert cfg.report.email.smtp_port == 2525


def test_env_smtp_port_invalid_raises(tmp_path: Path) -> None:
    with pytest.raises(SeoError) as exc:
        load_config(cwd=tmp_path, env={"LV24_SEO_SMTP_PORT": "not-a-number"})

    assert exc.value.code == CONFIG_INVALID
    assert "LV24_SEO_SMTP_PORT" in exc.value.message
    assert exc.value.next_action


def test_env_creates_provider_sections(tmp_path: Path) -> None:
    cfg = load_config(
        cwd=tmp_path,
        env={
            "LV24_SEO_GSC_PROPERTY": "sc-domain:env.example",
            "LV24_SEO_YANDEX_HOST_ID": "https:env.example:443",
            "LV24_SEO_YANDEX_METRIKA_COUNTER": "987654",
        },
    )

    assert cfg.providers.gsc is not None
    assert cfg.providers.gsc.property == "sc-domain:env.example"
    assert cfg.providers.gsc.auth == "keychain"
    assert cfg.providers.yandex_webmaster is not None
    assert cfg.providers.yandex_webmaster.host_id == "https:env.example:443"
    assert cfg.providers.yandex_metrika is not None
    assert cfg.providers.yandex_metrika.counter == "987654"
    assert cfg.providers.yandex_metrika.goals == []


def test_log_level_and_future_env_vars_are_ignored(tmp_path: Path) -> None:
    cfg = load_config(
        cwd=tmp_path,
        env={"LV24_SEO_LOG_LEVEL": "DEBUG", "LV24_SEO_FUTURE_FLAG": "1"},
    )
    assert cfg.report.locale == "ru"
    assert not hasattr(cfg, "log_level")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("[privacy]\ntelemetry = false\n", id="top-level"),
        pytest.param("[project]\nunknown = 1\n", id="project"),
        pytest.param('[providers.unknown]\nkey = "x"\n', id="provider"),
        pytest.param('[providers.gsc]\nproperty = "x"\ntypo = 1\n', id="provider-field"),
        pytest.param("[report.email]\nunknown = true\n", id="nested"),
    ],
)
def test_unknown_keys_raise_config_invalid(tmp_path: Path, body: str) -> None:
    path = _write_config(tmp_path, body)

    with pytest.raises(SeoError) as exc:
        load_config(path, cwd=tmp_path)

    assert exc.value.code == CONFIG_INVALID
    assert exc.value.next_action
    assert "invalid configuration" in exc.value.message


def test_validation_errors_do_not_echo_secret_inputs(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
        [project]
        name = "example.com"
        api_key = "super-secret-value"
        """,
    )

    with pytest.raises(SeoError) as exc:
        load_config(path, cwd=tmp_path)

    assert exc.value.code == CONFIG_INVALID
    assert "project.api_key" in exc.value.message
    assert "super-secret-value" not in exc.value.message
    assert "super-secret-value" not in str(exc.value)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    path = _write_config(tmp_path, '[project\nname = "broken"\n')

    with pytest.raises(SeoError) as exc:
        load_config(path, cwd=tmp_path)

    assert exc.value.code == CONFIG_INVALID
    assert "TOML" in exc.value.message
    assert exc.value.next_action


class _ConfigWithSecrets(SeoConfig):
    """Test double: a config subclass carrying secret-looking fields."""

    api_key: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


def test_redacted_dump_masks_secret_like_keys() -> None:
    cfg = _ConfigWithSecrets(
        api_key="super-secret",
        details={
            "refresh_token": "t",
            "client_secret": "s",
            "password": "p",
            "api-key": "k",
            "Authorization": "Bearer x",
            "chat_id": "123",
            "nested": {"apiKey": "z", "keep": 7},
            "items": [{"token": "nested"}],
        },
    )

    dump = redacted_dump(cfg)

    assert dump["api_key"] == "***"
    assert dump["details"]["refresh_token"] == "***"
    assert dump["details"]["client_secret"] == "***"
    assert dump["details"]["password"] == "***"
    assert dump["details"]["api-key"] == "***"
    assert dump["details"]["Authorization"] == "***"
    assert dump["details"]["chat_id"] == "123"
    assert dump["details"]["nested"] == {"apiKey": "***", "keep": 7}
    assert dump["details"]["items"] == [{"token": "***"}]

    # Redaction must not mutate the live config.
    assert cfg.api_key == "super-secret"
    assert cfg.details["refresh_token"] == "t"


def test_redacted_dump_preserves_non_secret_data(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
        [project]
        name = "example.com"

        [report.email]
        from = "seo@example.com"
        to = ["owner@example.com"]

        [storage]
        data_dir = "./data"
        """,
    )
    cfg = load_config(path, cwd=tmp_path)

    dump = redacted_dump(cfg)

    assert dump == cfg.model_dump(mode="json", by_alias=True)
    assert dump["report"]["email"]["from"] == "seo@example.com"
    assert "email_from" not in dump["report"]["email"]
    assert dump["storage"]["data_dir"] == "data"
