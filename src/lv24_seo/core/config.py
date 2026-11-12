"""Layered configuration for ``seo.toml`` and ``LV24_SEO_*`` environment variables.

Precedence, lowest to highest::

    model defaults  ->  seo.toml  ->  LV24_SEO_* environment

The file is discovered by :func:`resolve_config_path`: an explicit path wins, then
``./seo.toml``, then ``platformdirs.user_config_dir("lv24-seo")/config.toml``. A missing
file is not an error -- the defaults describe a runnable, read-only tool. Unknown TOML keys
are rejected so a typo fails loudly instead of silently disabling a setting.

Secrets are deliberately *not* part of this model: tokens and passwords live in
:mod:`lv24_seo.core.secrets`. :func:`redacted_dump` still masks any secret-looking key, so
a future field cannot leak through ``seo://config`` or CLI ``--json`` output.

Unsupported ``LV24_SEO_*`` variables are ignored for forward compatibility.
``LV24_SEO_LOG_LEVEL`` is accepted here but owned by the logging layer (WP0.7).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lv24_seo.core.errors import CONFIG_INVALID, SeoError

__all__ = [
    "BingConfig",
    "CrawlConfig",
    "CruxConfig",
    "EmailConfig",
    "GscConfig",
    "ProjectConfig",
    "ProvidersConfig",
    "PsiConfig",
    "ReportConfig",
    "SeoConfig",
    "StorageConfig",
    "TelegramConfig",
    "ThresholdsConfig",
    "YandexMetrikaConfig",
    "YandexWebmasterConfig",
    "load_config",
    "redacted_dump",
    "resolve_config_path",
]

APP_NAME = "lv24-seo"
CONFIG_FILENAME = "seo.toml"
USER_CONFIG_FILENAME = "config.toml"

_SECRET_KEY_RE = re.compile(r"token|secret|password|api[_-]?key|authorization", re.IGNORECASE)


class _StrictModel(BaseModel):
    """Reject unknown keys so a typo can never silently disable a setting."""

    model_config = ConfigDict(extra="forbid")


class ProjectConfig(_StrictModel):
    """Identity of the site this workspace reports on."""

    name: str = ""
    base_url: str = ""
    default_property: str = ""


class GscConfig(_StrictModel):
    """Google Search Console property and credential source."""

    property: str = ""
    auth: str = "keychain"


class YandexWebmasterConfig(_StrictModel):
    """Yandex Webmaster host id, for example ``https:example.com:443``."""

    host_id: str = ""


class YandexMetrikaConfig(_StrictModel):
    """Yandex Metrika counter and the goals that count as conversions."""

    counter: str = ""
    goals: list[int] = Field(default_factory=list)

    @field_validator("counter", mode="before")
    @classmethod
    def _stringify_counter(cls, value: Any) -> Any:
        # TOML allows an unquoted counter id; normalise it to the documented string form.
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value


class BingConfig(_StrictModel):
    """Bing Webmaster Tools site."""

    site: str = ""


class CruxConfig(_StrictModel):
    """Chrome UX Report origin."""

    origin: str = ""


class PsiConfig(_StrictModel):
    """PageSpeed Insights defaults."""

    strategy: Literal["mobile", "desktop"] = "mobile"
    urls: list[str] = Field(default_factory=list)


class ProvidersConfig(_StrictModel):
    """Optional provider blocks; absent means "not configured", not a failure."""

    gsc: GscConfig | None = None
    yandex_webmaster: YandexWebmasterConfig | None = None
    yandex_metrika: YandexMetrikaConfig | None = None
    bing: BingConfig | None = None
    crux: CruxConfig | None = None
    psi: PsiConfig | None = None


class ThresholdsConfig(_StrictModel):
    """Report movement thresholds, in percent or absolute positions."""

    clicks_pct: float = 10.0
    impressions_pct: float = 15.0
    position_abs: float = 1.5


class EmailConfig(_StrictModel):
    """Email delivery settings. ``from`` is a keyword, hence the alias."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    to: list[str] = Field(default_factory=list)
    email_from: str = Field(default="", alias="from")
    only_on_change: bool = True
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""


class TelegramConfig(_StrictModel):
    """Telegram delivery settings."""

    enabled: bool = False
    chat_id: str = ""
    severity: Literal["critical", "all"] = "critical"


class ReportConfig(_StrictModel):
    """Report locale, schedule and delivery channels."""

    locale: Literal["ru", "en", "zh"] = "ru"
    timezone: str = "Europe/Minsk"
    week_start: Literal["monday", "sunday"] = "monday"
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class CrawlConfig(_StrictModel):
    """Crawler limits. ``respect_robots`` stays on unless a user opts out explicitly.

    Crawling is serial with polite pacing (robots ``Crawl-delay`` between
    requests); ``concurrency`` is reserved for a future bounded worker pool and
    currently has no effect on execution.
    """

    max_urls: int = Field(default=500, ge=1)
    concurrency: int = Field(default=4, ge=1)
    respect_robots: bool = True
    user_agent: str = "lv24-seo/0.1"


class StorageConfig(_StrictModel):
    """Local data directory and raw-response retention."""

    data_dir: Path = Path("data")
    raw_retention_days: int = Field(default=90, ge=0)


class SeoConfig(_StrictModel):
    """Validated, fully-defaulted configuration for one SEO workspace."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


# Environment variables that map to one string leaf. Keep in sync with docs/config.md.
_ENV_STRING_PATHS: dict[str, tuple[str, ...]] = {
    "LV24_SEO_DATA_DIR": ("storage", "data_dir"),
    "LV24_SEO_GSC_PROPERTY": ("providers", "gsc", "property"),
    "LV24_SEO_YANDEX_HOST_ID": ("providers", "yandex_webmaster", "host_id"),
    "LV24_SEO_YANDEX_METRIKA_COUNTER": ("providers", "yandex_metrika", "counter"),
    "LV24_SEO_SMTP_HOST": ("report", "email", "smtp_host"),
    "LV24_SEO_SMTP_USER": ("report", "email", "smtp_user"),
    "LV24_SEO_SMTP_FROM": ("report", "email", "from"),
    "LV24_SEO_TELEGRAM_CHAT_ID": ("report", "telegram", "chat_id"),
    "LV24_SEO_REPORT_LOCALE": ("report", "locale"),
    "LV24_SEO_REPORT_TIMEZONE": ("report", "timezone"),
}
_ENV_MAIL_TO = "LV24_SEO_MAIL_TO"
_ENV_SMTP_PORT = "LV24_SEO_SMTP_PORT"


def resolve_config_path(path: Path | None = None, cwd: Path | None = None) -> Path | None:
    """Find the config file: explicit path, then ``./seo.toml``, then the user config dir.

    An explicit path must point to an existing file; otherwise ``CONFIG_INVALID`` is raised.
    Returns ``None`` when nothing exists, which means "use defaults" to :func:`load_config`.
    """
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise _invalid_config(
                f"config file not found: {candidate}",
                "create the file or pass an existing path",
            )
        if not candidate.is_file():
            raise _invalid_config(
                f"config path is not a file: {candidate}",
                "pass a path to a TOML file",
            )
        return candidate

    base = Path.cwd() if cwd is None else Path(cwd)
    local = base / CONFIG_FILENAME
    if local.is_file():
        return local

    user_config = Path(platformdirs.user_config_dir(APP_NAME)) / USER_CONFIG_FILENAME
    if user_config.is_file():
        return user_config
    return None


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> SeoConfig:
    """Build a :class:`SeoConfig` from defaults, TOML and environment overrides.

    ``env`` defaults to :data:`os.environ`; passing an explicit mapping keeps callers and
    tests hermetic. Environment values always win over the file.
    """
    resolved = resolve_config_path(path, cwd)
    data: dict[str, Any] = {} if resolved is None else _read_toml(resolved)
    _apply_env_overrides(data, os.environ if env is None else env)
    try:
        return SeoConfig.model_validate(data)
    except ValidationError as exc:
        raise _invalid_config(
            _format_validation_error(exc, resolved),
            "remove or fix the keys listed in the message (see docs/config.md)",
        ) from None


def redacted_dump(cfg: SeoConfig) -> dict[str, Any]:
    """Serialise a config for display, masking secret-looking keys with ``***``.

    Handles nested mappings and lists, so token/password values cannot leak through
    ``seo://config``, ``doctor --json`` or a Telegram diagnostic dump.
    """
    dumped = cfg.model_dump(mode="json", by_alias=True)
    return cast(dict[str, Any], _redact(dumped))


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError:
        raise _invalid_config(
            f"cannot read the config file at {path}",
            "check the path and file permissions",
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise _invalid_config(
            f"the config file at {path} is not valid TOML: {exc}",
            "fix the TOML syntax and try again",
        ) from None


def _apply_env_overrides(data: dict[str, Any], env: Mapping[str, str]) -> None:
    # Unsupported LV24_SEO_* variables (including LV24_SEO_LOG_LEVEL) are ignored on
    # purpose: config must not reject a shell that also exports logging or future flags.
    for name, target in _ENV_STRING_PATHS.items():
        if name in env:
            _set_nested(data, target, env[name].strip())

    if _ENV_MAIL_TO in env:
        _set_nested(data, ("report", "email", "to"), _parse_csv(env[_ENV_MAIL_TO]))

    if _ENV_SMTP_PORT in env:
        port = _parse_int(_ENV_SMTP_PORT, env[_ENV_SMTP_PORT])
        _set_nested(data, ("report", "email", "smtp_port"), port)


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for key in path[:-1]:
        child = node.get(key)
        if isinstance(child, dict):
            node = cast(dict[str, Any], child)
        else:
            created: dict[str, Any] = {}
            node[key] = created
            node = created
    node[path[-1]] = value


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise _invalid_config(
            f"{name} must be an integer, got {raw.strip()!r}",
            f"set {name} to a whole number, for example 587",
        ) from None


def _format_validation_error(exc: ValidationError, path: Path | None) -> str:
    # Build the message from locations and messages only: pydantic's `input` may echo a
    # secret that a user mistakenly put in the TOML file.
    details: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        details.append(f"{location}: {error['msg']}")
    remaining = exc.error_count() - len(details)
    if remaining > 0:
        details.append(f"and {remaining} more")
    where = f" in {path}" if path is not None else ""
    return f"invalid configuration{where}: " + "; ".join(details)


def _invalid_config(message: str, next_action: str) -> SeoError:
    return SeoError(code=CONFIG_INVALID, message=message, next_action=next_action)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***" if isinstance(key, str) and _SECRET_KEY_RE.search(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
