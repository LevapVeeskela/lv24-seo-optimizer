"""The full ``lv24-seo`` command tree (Wave 6).

Design rules:

* every command returns the single ``{ok, data, meta, warnings}`` envelope and exits with
  :func:`lv24_seo.core.envelope.envelope_exit_code`;
* ``--json`` prints exactly one JSON document; otherwise a compact plain-text summary is
  printed (no ANSI, no token values);
* secrets are never argv values: ``auth set`` reads stdin or a hidden prompt;
* the MCP SDK is never imported here. ``install`` writes host configuration and
  ``export tools`` reads the plain-data registry, nothing more.
"""

from __future__ import annotations

import html as html_module
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import Template
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from lv24_seo.automation.scheduler import ScheduleSpec
from lv24_seo.automation.scheduler import install as install_schedule
from lv24_seo.automation.scheduler import status as schedule_status
from lv24_seo.automation.scheduler import uninstall as uninstall_schedule
from lv24_seo.cli import configure_stdio, emit, run_envelope, tool_version
from lv24_seo.cli.auth import auth_app
from lv24_seo.cli.backup_cmd import backup_app
from lv24_seo.cli.logs_cmd import logs_app
from lv24_seo.cli.portfolio_cmd import portfolio_app
from lv24_seo.cli.tasks_cmd import tasks_app
from lv24_seo.core.config import (
    SeoConfig,
    data_dir_path,
    load_config,
    redacted_dump,
    resolve_config_path,
)
from lv24_seo.core.envelope import envelope_exit_code, fail, ok, partial
from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    CONFIG_INVALID,
    CONFIRM_REQUIRED,
    INTERNAL,
    INVALID_INPUT,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.logging import configure_logging_from_env
from lv24_seo.core.secrets import OAUTH_SECRET_NAMES, SECRET_NAMES, SecretStore
from lv24_seo.core.write_gate import WRITE_SECRET_ENV, WriteGate, WritePlan
from lv24_seo.crawl.fetcher import Fetcher
from lv24_seo.crawl.sitemap import MAX_BYTES as SITEMAP_MAX_BYTES
from lv24_seo.crawl.sitemap import SitemapReader
from lv24_seo.mcp.registry import tools_as_json
from lv24_seo.providers.bing import (
    indexnow_submit,
    validate_indexnow_key,
    validate_indexnow_key_location,
)
from lv24_seo.providers.gsc.auth import service_account_file_problem
from lv24_seo.report.i18n import normalize_locale
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_html, render_text
from lv24_seo.services import SeoService
from lv24_seo.store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Free-first SEO control plane for humans and AI agents.",
)


@app.callback()
def _configure_cli() -> None:
    """Run before every command so redirected output stays UTF-8 on Windows."""
    configure_stdio()


config_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Inspect configuration.")
gsc_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Google Search Console.")
yandex_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Yandex Webmaster.")
metrika_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Yandex Metrika organic traffic, goals and dimensions (read-only).",
)
sitemap_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Sitemap submission (write, gated)."
)
indexnow_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="IndexNow submission (write, gated)."
)
bing_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Bing URL submission, quota and sitemap status.",
)
indexing_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Quota-aware index checks and URL submission (write gated).",
)
report_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Build reports.")
cwv_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Core Web Vitals: CrUX field/history, PSI lab/audits.",
)
schedule_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Host scheduler jobs.")
export_app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Export machine contracts."
)

app.add_typer(auth_app, name="auth", help="Manage provider tokens.")
app.add_typer(config_app, name="config", help="Inspect configuration.")
app.add_typer(gsc_app, name="gsc", help="Google Search Console.")
app.add_typer(yandex_app, name="yandex", help="Yandex Webmaster.")
app.add_typer(
    metrika_app,
    name="metrika",
    help="Yandex Metrika organic traffic, goals and dimensions (read-only).",
)
app.add_typer(sitemap_app, name="sitemap", help="Sitemap submission (write, gated).")
app.add_typer(indexnow_app, name="indexnow", help="IndexNow submission (write, gated).")
app.add_typer(
    bing_app, name="bing", help="Bing Webmaster reads, URL submission, quota and sitemaps."
)
app.add_typer(
    indexing_app,
    name="indexing",
    help="Quota-aware index checks and URL submission (write gated).",
)
app.add_typer(report_app, name="report", help="Build reports.")
app.add_typer(cwv_app, name="cwv", help="Core Web Vitals: collect, audits and history.")
app.add_typer(schedule_app, name="schedule", help="Host scheduler jobs.")
app.add_typer(export_app, name="export", help="Export machine contracts.")
app.add_typer(logs_app, name="logs", help="Analyze local nginx JSON access logs (read-only).")
app.add_typer(tasks_app, name="tasks", help="Track findings and their fix lifecycle.")
app.add_typer(backup_app, name="backup", help="Create a portable local backup.")
app.add_typer(
    portfolio_app,
    name="portfolio",
    help="Roll up several local projects from one directory (read-only).",
)


@app.callback()
def _configure_logging() -> None:
    """Install the redacting log handler when ``LV24_SEO_LOG_LEVEL`` asks for it.

    This is a process entry point, not a library import: the CLI never reconfigures the root
    logger unless the operator opted in, so embedding applications keep their own handlers.
    """
    configure_logging_from_env()


_DB_FILENAME = "seo.sqlite3"  # canonical: lv24_seo.composition.DB_FILENAME
_MAX_HUMAN_ROWS = 20
_MAX_CELL = 100
_STRICT_HELP = "Exit 5 when the response is truncated (CI gate); meta.strict is always set."
# IndexNow protocol caps: 10k URLs per POST; at most five child sitemaps are followed so one
# command cannot fan out into an unbounded crawl of a sitemap tree.
_INDEXNOW_URL_LIMIT = 10_000
_INDEXNOW_SITEMAP_DOCS = 6
_BING_SUBMIT_BATCH = 500

_SCHEDULE_CRONS: dict[str, str] = {
    "weekly": "0 6 * * 1",
    "daily": "0 6 * * *",
    "indexing": "0 4 * * *",
}

_MCP_ARGS: tuple[str, ...] = ("--from", "lv24-seo", "lv24-seo", "mcp", "serve")
_MCP_CLIENTS: frozenset[str] = frozenset({"claude", "opencode", "vscode", "cursor"})

_GSC_COLUMNS = (
    "query",
    "page",
    "country",
    "device",
    "date",
    "clicks",
    "impressions",
    "ctr",
    "position",
)
_YANDEX_COLUMNS = ("query", "shows", "clicks", "position", "date")
_METRIKA_PAGE_COLUMNS = (
    "startURLPath",
    "visits",
    "users",
    "bounceRate",
    "avgVisitDurationSeconds",
    "pageDepth",
    "pageviews",
)
_METRIKA_QUERY_COLUMNS = ("lastSearchPhrase", "lastSearchEngineRoot", "visits", "users")
_METRIKA_DEVICE_COLUMNS = (
    "deviceCategory",
    "visits",
    "users",
    "bounceRate",
    "avgVisitDurationSeconds",
    "pageviews",
)
_METRIKA_COUNTRY_COLUMNS = (
    "regionCountry",
    "visits",
    "users",
    "bounceRate",
    "avgVisitDurationSeconds",
    "pageviews",
)
_METRIKA_ENGAGEMENT_COLUMNS = (
    "bounceRate",
    "avgVisitDurationSeconds",
    "pageDepth",
    "pageviews",
    "visits",
    "users",
)
_METRIKA_BYTIME_COLUMNS = ("date", "visits", "users")

_TEMPLATE = Template(
    """# lv24-seo workspace configuration (generated by `lv24-seo init`).
# Docs: docs/config.md. Secrets never belong here: use `lv24-seo auth set`
# or the matching LV24_SEO_* environment variable.

[project]
name = $name
base_url = $base_url
default_property = $default_property

# Google Search Console. Store the token with `lv24-seo auth set --provider gsc --stdin`.
[providers.gsc]
property = $gsc_property
auth = "keychain"

# Yandex Webmaster. Store the token with `lv24-seo auth set --provider yandex --stdin`.
[providers.yandex_webmaster]
host_id = $yandex_host_id

# Yandex Metrika token: LV24_SEO_YANDEX_METRIKA_TOKEN.
[providers.yandex_metrika]
counter = ""
goals = []

# Bing Webmaster. The API key is a secret (`lv24-seo auth set --provider bing`).
# indexnow_key and indexnow_key_location are public identifiers: the key is served openly
# at the key location (default https://<host>/<key>.txt) and sent in each IndexNow request.
[providers.bing]
site = $bing_site
indexnow_key = ""
indexnow_key_location = ""

[providers.crux]
origin = $crux_origin

[providers.psi]
strategy = "mobile"
urls = $psi_urls

[report]
locale = "ru"
timezone = "Europe/Minsk"
week_start = "monday"
thresholds = { clicks_pct = 10, impressions_pct = 15, position_abs = 1.5 }

[report.email]
enabled = false
to = []
from = "seo@example.com"
only_on_change = true
smtp_host = ""
smtp_port = 587
smtp_user = ""

[report.telegram]
enabled = false
chat_id = ""
severity = "critical"

[crawl]
max_urls = 500
concurrency = 4
respect_robots = true
user_agent = "lv24-seo/0.1"

[storage]
data_dir = $data_dir
raw_retention_days = 90

# Multi-property ([[sites]]) and [privacy] are described in docs/config.md but are not part
# of the current strict schema, so they stay commented out until the model supports them.
# [privacy]
# telemetry = false
"""
)


# -- discovery -------------------------------------------------------------------------------


@app.command()
def version(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Accepted for uniformity; version needs no config."),
    ] = None,
) -> None:
    """Print the tool version."""
    del config_path
    emit(
        ok({"name": "lv24-seo", "version": tool_version()}),
        as_json=as_json,
        lines=[f"lv24-seo {tool_version()}"],
    )


@app.command()
def doctor(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to check.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Check python, config, data directory and tokens. No network calls."""

    def action() -> dict[str, Any]:
        resolved = resolve_config_path(config_path)
        config = load_config(config_path)
        data_dir = data_dir_path(config)
        statuses = SecretStore(data_dir=config.storage.data_dir).status(
            [*SECRET_NAMES, *OAUTH_SECRET_NAMES]
        )
        present = [status.name for status in statuses if status.present]
        scope_note = _data_dir_scope_note(data_dir, resolved)
        gsc = config.providers.gsc
        gsc_file = gsc.service_account_file.strip() if gsc is not None else ""
        gsc_problem = service_account_file_problem(gsc_file)
        oauth_grant = (
            bool(config.auth.gsc.client_id.strip())
            and "gsc_refresh_token" in present
            and "gsc_oauth_client_secret" in present
        )
        if gsc_file:
            gsc_ok = gsc_problem is None
            gsc_detail = (
                f"service account file: {Path(gsc_file).expanduser()}"
                if gsc_ok
                else str(gsc_problem)
            )
        elif oauth_grant:
            gsc_ok = True
            gsc_detail = "OAuth refresh token present (gsc_refresh_token)"
        else:
            if "gsc_token" in present:
                gsc_ok = True
                gsc_detail = "token present (gsc_token)"
            elif gsc is None:
                gsc_ok = True
                gsc_detail = "not configured (optional)"
            else:
                gsc_ok = False
                gsc_detail = (
                    "credential missing; set LV24_SEO_GSC_SERVICE_ACCOUNT_FILE or run "
                    "`lv24-seo auth set --provider gsc --stdin`"
                )
        gsc_file_ok = bool(gsc_file) and gsc_problem is None
        checks = [
            {
                "id": "python",
                "ok": sys.version_info >= (3, 11),
                "detail": sys.version.split()[0],
            },
            {
                "id": "platform",
                "ok": True,
                "detail": f"{sys.platform}",
            },
            {
                "id": "config",
                "ok": resolved is not None,
                "detail": (
                    str(resolved) if resolved is not None else "not found; run `lv24-seo init`"
                ),
            },
            {
                "id": "cwd_writable",
                "ok": _is_writable(Path.cwd()),
                "detail": str(Path.cwd()),
            },
            {
                "id": "data_dir",
                "ok": data_dir.is_dir() and _is_writable(data_dir),
                "detail": str(data_dir),
            },
            {
                "id": "tokens",
                "ok": bool(present) or gsc_file_ok,
                "detail": (
                    f"{len(present)} of {len(statuses)} present: {', '.join(present)}"
                    if present
                    else (
                        "GSC uses a service-account file; no stored secrets"
                        if gsc_file_ok
                        else "none present; run `lv24-seo auth status`"
                    )
                ),
            },
            {
                "id": "gsc_auth",
                "ok": gsc_ok,
                "detail": gsc_detail,
            },
        ]
        # A declared data directory is part of the workspace contract: when the config file or
        # LV24_SEO_DATA_DIR names one, every store-backed command needs it. Reporting
        # `healthy: true` while the directory is missing or unusable made the CI gate useless.
        data_dir_declared = resolved is not None or bool(
            os.environ.get("LV24_SEO_DATA_DIR", "").strip()
        )
        fatal: tuple[str, ...] = ("python", "cwd_writable")
        if data_dir_declared:
            fatal = (*fatal, "data_dir")
        healthy = all(check["ok"] for check in checks if check["id"] in fatal)
        meta = {
            "config": None if resolved is None else str(resolved),
            "data_dir": str(data_dir),
            "data_dir_source": "config" if data_dir.is_absolute() else "working directory",
            "data_dir_declared": data_dir_declared,
        }
        # A relative data directory is not an error (it is the historical default), but with a
        # config file present it is ambiguous, so it is reported through the envelope warning
        # channel instead of a failing check that would contradict `healthy: true`.
        warnings = [scope_note] if scope_note is not None else []
        if healthy:
            return ok({"healthy": True, "checks": checks}, meta=meta, warnings=warnings)
        return fail(
            SeoError(
                code=INTERNAL,
                message="environment check failed",
                next_action="fix the failing checks and rerun `lv24-seo doctor`",
            ),
            meta={**meta, "healthy": False, "checks": checks},
            warnings=warnings,
        )

    payload = run_envelope(action)
    checks = _checks(payload)
    emit(payload, as_json=as_json, lines=_doctor_lines(checks, bool(payload.get("ok"))))


@app.command()
def init(
    property_url: Annotated[
        str | None,
        typer.Option(
            "--property",
            help="Site URL or GSC property, for example https://example.com.",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Data directory (default ./data).")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing seo.toml.")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Destination config file.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Write a commented seo.toml and the data directory."""

    def action() -> dict[str, Any]:
        destination = (
            Path(config_path).expanduser() if config_path is not None else Path.cwd() / "seo.toml"
        )
        if destination.exists() and destination.is_dir():
            raise SeoError(
                code=INVALID_INPUT,
                message=f"{destination} is a directory",
                next_action="pass a file path to --config",
            )
        existed = destination.exists()
        if existed and not force:
            raise SeoError(
                code=CONFIG_INVALID,
                message=f"{destination} already exists",
                next_action="pass --force to overwrite it",
            )

        project = _project_from_property(property_url)
        relative_data = Path("data") if data_dir is None else Path(data_dir)
        target_data_dir = (
            relative_data if relative_data.is_absolute() else destination.parent / relative_data
        )
        _ensure_dir(target_data_dir)
        _write_text(
            destination,
            _render_template(project=project, data_dir=_display_path(relative_data)),
        )
        return ok(
            {
                "config_path": str(destination),
                "data_dir": str(target_data_dir),
                "overwritten": existed,
                "project": project,
            },
            meta={"template": "docs/config.md"},
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_init_lines(payload))


@config_app.command("show")
def config_show(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to show.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Print the effective config with secret-looking keys redacted."""

    def action() -> dict[str, Any]:
        resolved = resolve_config_path(config_path)
        config = load_config(config_path)
        return ok(
            {
                "path": None if resolved is None else str(resolved),
                "data_dir": str(data_dir_path(config)),
                "config": redacted_dump(config),
            },
            meta={"redacted": True},
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_config_lines(payload))


# -- data ------------------------------------------------------------------------------------


@app.command()
def properties(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List configured providers, their target and whether a token is present."""
    payload = _run(config_path, lambda service, config: service.properties())
    emit(payload, as_json=as_json, lines=_properties_lines(payload))


@app.command()
def stats(
    days: Annotated[int, typer.Option("--days", min=1, help="Current period length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Per-provider dashboard metrics for one period (read-only, no store)."""
    payload = _run(
        config_path,
        lambda service, config: service.stats(days=days, lag_days=lag_days),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_stats_lines(payload))


@gsc_app.command("query")
def gsc_query(
    start: Annotated[date, typer.Option("--start", help="First day, YYYY-MM-DD.")],
    end: Annotated[date, typer.Option("--end", help="Last day, YYYY-MM-DD.")],
    dimensions: Annotated[
        str,
        typer.Option(
            "--dimensions",
            help="Comma-separated: query,page,country,device,date,searchAppearance,hour.",
        ),
    ] = "query",
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    search_type: Annotated[
        str,
        typer.Option(
            "--type",
            help="Search type: web,image,video,news,discover,googleNews,webMultimodal.",
        ),
    ] = "web",
    filters: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            help="Filter dimension:operator:expression; repeat for an AND group.",
        ),
    ] = None,
    filter_lists: Annotated[
        list[str] | None,
        typer.Option(
            "--filter-list",
            help="Multi-value filter dimension:expr1,expr2 (country/device); repeatable.",
        ),
    ] = None,
    data_state: Annotated[
        str,
        typer.Option("--data-state", help="Data freshness: all, final or hourly_all."),
    ] = "",
    aggregation: Annotated[
        str,
        typer.Option(
            "--aggregation",
            help="auto, byPage, byProperty or byNewsShowcasePanel.",
        ),
    ] = "",
    search_appearance: Annotated[
        str,
        typer.Option(
            "--search-appearance",
            help="Filter by a search appearance, e.g. AMP_BLUE_LINK.",
        ),
    ] = "",
    branded: Annotated[
        str,
        typer.Option("--branded", help="Branded filter: branded or non_branded."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Query GSC search analytics for one date range."""
    dims = tuple(part.strip() for part in dimensions.split(",") if part.strip())

    def operation(service: SeoService, config: SeoConfig) -> dict[str, Any]:
        query_filters = _gsc_filter_args(filters, search_appearance, branded)
        query_filter_lists = _gsc_filter_list_args(filter_lists)
        return service.gsc_query(
            start=start,
            end=end,
            dimensions=dims,
            limit=limit,
            search_type=search_type,
            filters=query_filters or None,
            filter_lists=query_filter_lists or None,
            data_state=data_state,
            aggregation=aggregation,
        )

    payload = _run(config_path, operation, persist=True, strict=strict)
    emit(payload, as_json=as_json, lines=_gsc_lines(payload))


@gsc_app.command("inspect")
def gsc_inspect(
    url: Annotated[str, typer.Option("--url", help="Absolute URL to inspect (one per call).")],
    language: Annotated[
        str,
        typer.Option("--language", help="BCP-47 language for localized states (default en-US)."),
    ] = "en-US",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Inspect one URL with GSC URL Inspection (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.gsc_inspect(url, language=language),
    )
    emit(payload, as_json=as_json, lines=_gsc_inspect_lines(payload))


@gsc_app.command("sitemaps")
def gsc_sitemaps(
    sitemap_index: Annotated[
        str,
        typer.Option(
            "--sitemap-index",
            help="Sitemap index URL whose contents summary should be added.",
        ),
    ] = "",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List the property's sitemaps as Google reports them (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.gsc_sitemaps(sitemap_index=sitemap_index),
    )
    emit(payload, as_json=as_json, lines=_gsc_sitemaps_lines(payload))


@gsc_app.command("sitemap")
def gsc_sitemap(
    path: Annotated[
        str,
        typer.Option("--path", help="Absolute sitemap (or sitemap index) URL."),
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read one sitemap entry, including a sitemap index's contents summary."""
    payload = _run(
        config_path,
        lambda service, config: service.gsc_sitemap(path=path),
    )
    emit(payload, as_json=as_json, lines=_gsc_sitemap_lines(payload))


@yandex_app.command("queries")
def yandex_queries(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Top Yandex queries for the configured host."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_queries(limit=limit),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_queries_lines(payload))


@yandex_app.command("indexing")
def yandex_indexing(
    date_from: Annotated[
        date | None,
        typer.Option("--date-from", help="First day of the history window, YYYY-MM-DD."),
    ] = None,
    date_to: Annotated[
        date | None,
        typer.Option("--date-to", help="Last day of the history window, YYYY-MM-DD."),
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Indexing history, search events, problems and counters for the host."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_indexing(
            date_from=date_from,
            date_to=date_to,
        ),
        persist=True,
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_indexing_lines(payload))


@yandex_app.command("samples")
def yandex_samples(
    kind: Annotated[
        str,
        typer.Option("--kind", help="Sample kind: indexing or events (default indexing)."),
    ] = "indexing",
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum rows to return (1..1000)."),
    ] = 100,
    offset: Annotated[
        int,
        typer.Option("--offset", min=0, help="First provider row."),
    ] = 0,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read indexing or search-event samples (bounded pages of 100 rows)."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_samples(
            kind=kind,
            limit=limit,
            offset=offset,
        ),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_samples_lines(payload))


@yandex_app.command("sitemaps")
def yandex_sitemaps(
    sitemap_id: Annotated[
        str,
        typer.Option("--id", help="Read one sitemap by its provider id."),
    ] = "",
    parent_id: Annotated[
        str,
        typer.Option("--parent-id", help="List the children of one sitemap index."),
    ] = "",
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, help="Maximum rows (1..500)."),
    ] = 100,
    from_: Annotated[
        str,
        typer.Option("--from", help="Provider paging cursor (a sitemap id)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List Yandex sitemaps or read one by id (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_sitemaps(
            sitemap_id=sitemap_id,
            parent_id=parent_id,
            limit=limit,
            from_=from_,
        ),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_sitemaps_lines(payload))


@yandex_app.command("history")
def yandex_history(
    kind: Annotated[
        str,
        typer.Option("--kind", help="sqi, in-search, queries or query (default sqi)."),
    ] = "sqi",
    query_id: Annotated[
        str,
        typer.Option("--query-id", help="Query id for --kind query."),
    ] = "",
    date_from: Annotated[
        date | None,
        typer.Option("--date-from", help="First day of the window, YYYY-MM-DD."),
    ] = None,
    date_to: Annotated[
        date | None,
        typer.Option("--date-to", help="Last day of the window, YYYY-MM-DD."),
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read one Yandex history series (SQI, in-search, all queries or one query)."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_history(
            kind=kind,
            query_id=query_id,
            date_from=date_from,
            date_to=date_to,
        ),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_history_lines(payload))


@yandex_app.command("queue")
def yandex_queue(
    task_id: Annotated[
        str,
        typer.Option("--id", help="Read one recrawl task by id."),
    ] = "",
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, help="Maximum rows (1..500)."),
    ] = 100,
    offset: Annotated[
        int,
        typer.Option("--offset", min=0, help="First provider row."),
    ] = 0,
    date_from: Annotated[
        date | None,
        typer.Option("--date-from", help="First day of the window, YYYY-MM-DD."),
    ] = None,
    date_to: Annotated[
        date | None,
        typer.Option("--date-to", help="Last day of the window, YYYY-MM-DD."),
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read the Yandex recrawl queue or one task by id (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.yandex_queue(
            task_id=task_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        ),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_yandex_queue_lines(payload))


# -- Yandex Metrika reads --------------------------------------------------------------------


@metrika_app.command("pages")
def metrika_pages_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic landing pages with visits, users and engagement (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_pages(days=days, lag_days=lag_days, limit=limit),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika pages", _METRIKA_PAGE_COLUMNS),
    )


@metrika_app.command("goals")
def metrika_goals_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    dimension: Annotated[
        str,
        typer.Option("--dimension", help="Break goals down by page or source (default: total)."),
    ] = "",
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Goal reaches, visits, users and conversion rate from configured goal ids."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_goals(
            days=days, lag_days=lag_days, dimension=dimension, limit=limit
        ),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika goals", _metrika_goal_columns(payload)),
    )


@metrika_app.command("queries")
def metrika_queries_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic search phrases and the search engine that referred them."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_queries(days=days, lag_days=lag_days, limit=limit),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika queries", _METRIKA_QUERY_COLUMNS),
    )


@metrika_app.command("devices")
def metrika_devices_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic visits by device category (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_devices(days=days, lag_days=lag_days, limit=limit),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika devices", _METRIKA_DEVICE_COLUMNS),
    )


@metrika_app.command("countries")
def metrika_countries_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Maximum rows.")] = 100,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic visits by country (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_countries(
            days=days, lag_days=lag_days, limit=limit
        ),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika countries", _METRIKA_COUNTRY_COLUMNS),
    )


@metrika_app.command("engagement")
def metrika_engagement_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic engagement totals: bounce rate, duration, page depth, pageviews."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_engagement(days=days, lag_days=lag_days),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika engagement", _METRIKA_ENGAGEMENT_COLUMNS),
    )


@metrika_app.command("bytime")
def metrika_bytime_command(
    days: Annotated[int, typer.Option("--days", min=1, help="Window length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    group: Annotated[str, typer.Option("--group", help="Interval: day, week or month.")] = "day",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Organic visits and users over time via ``stat/v1/data/bytime`` (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.metrika_bytime(days=days, lag_days=lag_days, group=group),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_metrika_lines(payload, "metrika bytime", _METRIKA_BYTIME_COLUMNS),
    )


# -- writes (ADR-0003: plan -> confirm token -> apply) ---------------------------------------


@sitemap_app.command("submit")
def sitemap_submit(
    provider: Annotated[
        str,
        typer.Option("--provider", help="Sitemap provider: gsc or bing (default gsc)."),
    ] = "gsc",
    property_id: Annotated[
        str,
        typer.Option(
            "--property",
            help="GSC property id (required for gsc) or the Bing site URL.",
        ),
    ] = "",
    url: Annotated[
        str,
        typer.Option(
            "--url",
            help="Absolute sitemap URL; bing defaults to <project.base_url>/sitemap.xml.",
        ),
    ] = "",
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Opt in to the write; without it the command refuses."),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="confirm_token returned by the dry run."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Config file; its data directory holds the write ledger."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Submit a sitemap to Google Search Console or Bing.

    Pass ``--allow-write`` (and have ``LV24_SEO_WRITE_SECRET`` set) to plan; apply with
    ``--allow-write --confirm TOKEN``.
    """

    def prepare() -> tuple[WritePlan, Callable[[WritePlan], Any] | None]:
        config = load_config(config_path)
        target = SeoService(config).sitemap_target(
            provider=provider, property=property_id, sitemap_url=url
        )
        name = target["provider"]
        site = target["site_url"]
        sitemap_url = target["sitemap_url"]
        payload: dict[str, Any] = {
            "property": site,
            "url": sitemap_url,
            "action": "submit",
        }
        if name == "bing":
            payload["provider"] = "bing"
            summary = f"submit sitemap {sitemap_url} to {site} via bing"
        else:
            summary = f"submit sitemap {sitemap_url} to {site}"
        plan = WritePlan(
            action="sitemap_submit",
            target=site,
            summary=summary,
            payload=payload,
        )

        def execute(_plan: WritePlan) -> Any:
            current = load_config(config_path)
            return _service_write_result(
                SeoService(current).submit_sitemap(
                    provider=name,
                    property=site,
                    sitemap_url=sitemap_url,
                    action="submit",
                )
            )

        return plan, execute

    payload = _write_command(
        prepare, allow_write=allow_write, confirm=confirm, config_path=config_path
    )
    emit(payload, as_json=as_json, lines=_write_lines(payload))


@yandex_app.command("recrawl")
def yandex_recrawl(
    url: Annotated[list[str], typer.Option("--url", help="URL to queue; repeat the flag.")],
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Opt in to the write; without it the command refuses."),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="confirm_token returned by the dry run."),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Queue URLs for Yandex recrawl.

    Pass ``--allow-write`` (and have ``LV24_SEO_WRITE_SECRET`` set) to plan; apply with
    ``--allow-write --confirm TOKEN``. Yandex has no batch endpoint: apply sends one request
    per URL, reports each ``task_id`` and stops honestly when the daily quota is exhausted.
    """

    def prepare() -> tuple[WritePlan, Callable[[WritePlan], Any] | None]:
        config = load_config(config_path)
        webmaster = config.providers.yandex_webmaster
        host_id = webmaster.host_id.strip() if webmaster is not None else ""
        if not host_id:
            raise SeoError(
                code=INVALID_INPUT,
                message="Yandex Webmaster host_id is not configured",
                next_action="set providers.yandex_webmaster.host_id in seo.toml",
            )
        target_urls = _absolute_urls(url, name="--url")
        plan = WritePlan(
            action="recrawl",
            target=host_id,
            summary=f"queue {len(target_urls)} URL(s) for recrawl on {host_id}",
            payload={"host_id": host_id, "urls": target_urls},
        )

        def execute(plan: WritePlan) -> Any:
            current = load_config(config_path)
            payload = plan.payload
            urls = [str(value) for value in payload.get("urls") or []]
            return _service_write_result(
                SeoService(current).recrawl_request(
                    host_id=str(payload.get("host_id") or host_id),
                    urls=urls,
                )
            )

        return plan, execute

    payload = _write_command(
        prepare, allow_write=allow_write, confirm=confirm, config_path=config_path
    )
    emit(payload, as_json=as_json, lines=_write_lines(payload))


@indexnow_app.command("submit")
def indexnow_submit_command(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Bare hostname, for example example.com (default: providers.bing.site host).",
        ),
    ] = "",
    key: Annotated[
        str,
        typer.Option(
            "--key",
            help=(
                "IndexNow key published at https://<host>/<key>.txt "
                "(default: providers.bing.indexnow_key)."
            ),
        ),
    ] = "",
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="URL to submit; repeat the flag. Merged with --from-sitemap."),
    ] = None,
    from_sitemap: Annotated[
        bool,
        typer.Option("--from-sitemap", help="Read the URL list from the project sitemap."),
    ] = False,
    sitemap_url: Annotated[
        str,
        typer.Option(
            "--sitemap-url",
            help=(
                "Absolute sitemap URL (default: <project.base_url>/sitemap.xml); "
                "implies --from-sitemap."
            ),
        ),
    ] = "",
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Opt in to the write; without it the command refuses."),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="confirm_token returned by the dry run."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Config file; its data directory holds the write ledger."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Submit URLs to IndexNow.

    Pass ``--allow-write`` (and have ``LV24_SEO_WRITE_SECRET`` set) to plan; apply with
    ``--allow-write --confirm TOKEN``.

    ``--from-sitemap`` reads the URL list from the project sitemap during the plan and
    re-fetches it on apply; a changed sitemap invalidates the plan token.
    """
    context: dict[str, Any] = {}

    def prepare() -> tuple[WritePlan, Callable[[WritePlan], Any] | None]:
        config = load_config(config_path)
        bing = config.providers.bing
        configured_site = bing.site.strip() if bing is not None else ""
        host_value = _bare_host(host) if host.strip() else _host_from_site(configured_site)
        if not host_value:
            raise SeoError(
                code=INVALID_INPUT,
                message="IndexNow host is required",
                next_action=("pass --host example.com or set providers.bing.site in seo.toml"),
            )
        key_value = key.strip() or (bing.indexnow_key.strip() if bing is not None else "")
        if not key_value:
            raise SeoError(
                code=AUTH_REQUIRED,
                message="IndexNow key is missing",
                next_action=(
                    f"host the key file at https://{host_value}/<key>.txt and set "
                    "providers.bing.indexnow_key in seo.toml (or pass --key)"
                ),
            )
        key_value = _indexnow_key(key_value)
        configured_location = bing.indexnow_key_location.strip() if bing is not None else ""
        key_location = _absolute_url(
            configured_location or f"https://{host_value}/{key_value}.txt",
            name="providers.bing.indexnow_key_location",
        )
        explicit_urls = _url_list(url or [], name="--url")
        use_sitemap = from_sitemap or bool(sitemap_url.strip())
        if not use_sitemap and not explicit_urls:
            raise SeoError(
                code=INVALID_INPUT,
                message="at least one URL is required",
                next_action="pass --url https://example.com/page or --from-sitemap",
            )

        sitemap_target = ""
        sitemap_urls: list[str] = []
        sitemap_errors: list[str] = []
        sitemap_truncated = False
        off_host = 0
        if use_sitemap:
            sitemap_target = _indexnow_sitemap_url(sitemap_url, config)
            inventory = _read_sitemap_inventory(sitemap_target, host_value)
            sitemap_errors = inventory.errors
            sitemap_truncated = inventory.truncated
            sitemap_urls, off_host = _filter_host_urls(inventory.urls, host_value)
            if not sitemap_urls and not explicit_urls:
                if sitemap_errors:
                    raise SeoError(
                        code=UPSTREAM_UNAVAILABLE,
                        message=(
                            "could not read the sitemap: " + ", ".join(sorted(set(sitemap_errors)))
                        ),
                        next_action=(
                            "check the sitemap URL, or pass explicit --url values instead"
                        ),
                        retryable=True,
                    )
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"the sitemap has no URLs for {host_value}",
                    next_action="check the sitemap or pass explicit --url values",
                )

        discovered = _dedupe([*explicit_urls, *sitemap_urls])
        total = len(discovered)
        targets = discovered[:_INDEXNOW_URL_LIMIT]
        # Fail the dry run, not the apply: the key file's directory bounds the URL set.
        validate_indexnow_key_location(key_location=key_location, host=host_value, urls=targets)
        truncated = sitemap_truncated or len(discovered) > len(targets)
        limitations = _sitemap_write_limitations(
            action="indexnow",
            sitemap_target=sitemap_target,
            sitemap_errors=sitemap_errors,
            sitemap_truncated=sitemap_truncated,
            off_host=off_host,
            host=host_value,
            dropped=total - len(targets),
        )
        context.update({"truncated": truncated, "total": total, "limitations": limitations})
        plan = WritePlan(
            action="indexnow",
            target=host_value,
            summary=f"submit {len(targets)} URL(s) to IndexNow for {host_value}",
            payload={
                "host": host_value,
                "key": key_value,
                "key_location": key_location,
                "urls": targets,
                **({"sitemap_url": sitemap_target} if sitemap_target else {}),
            },
        )

        def execute(_plan: WritePlan) -> Any:
            final_urls = targets
            if sitemap_target:
                current = _read_sitemap_inventory(sitemap_target, host_value)
                current_urls, _ = _filter_host_urls(current.urls, host_value)
                if current_urls != sitemap_urls:
                    raise SeoError(
                        code=CONFIRM_REQUIRED,
                        message="the sitemap changed since the dry run; the plan token is stale",
                        next_action=(
                            "rerun the dry run to review the new URL set, then apply the "
                            "fresh confirm_token"
                        ),
                        retryable=False,
                    )
                refreshed = _dedupe([*explicit_urls, *current_urls])
                final_urls = refreshed[:_INDEXNOW_URL_LIMIT]
                context["truncated"] = sitemap_truncated or len(refreshed) > len(final_urls)
                context["total"] = len(refreshed)
                context["limitations"] = _sitemap_write_limitations(
                    action="indexnow",
                    sitemap_target=sitemap_target,
                    sitemap_errors=current.errors,
                    sitemap_truncated=current.truncated,
                    off_host=off_host,
                    host=host_value,
                    dropped=len(refreshed) - len(final_urls),
                )
            return indexnow_submit(
                host=host_value,
                key=key_value,
                urls=final_urls,
                key_location=key_location,
            )

        return plan, execute

    payload = _write_command(
        prepare,
        allow_write=allow_write,
        confirm=confirm,
        config_path=config_path,
        context=context,
    )
    emit(payload, as_json=as_json, lines=_write_lines(payload))


# -- Bing reads and URL submission -----------------------------------------------------------


@bing_app.command("submit")
def bing_submit_command(
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="URL to submit; repeat the flag. Merged with --from-sitemap."),
    ] = None,
    from_sitemap: Annotated[
        bool,
        typer.Option("--from-sitemap", help="Read the URL list from the project sitemap."),
    ] = False,
    sitemap_url: Annotated[
        str,
        typer.Option(
            "--sitemap-url",
            help=(
                "Absolute sitemap URL (default: <project.base_url>/sitemap.xml); "
                "implies --from-sitemap."
            ),
        ),
    ] = "",
    site: Annotated[
        str,
        typer.Option(
            "--site",
            help="Bing site URL, for example https://example.com (default: providers.bing.site).",
        ),
    ] = "",
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Opt in to the write; without it the command refuses."),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="confirm_token returned by the dry run."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Config file; its data directory holds the write ledger."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Submit URLs to Bing Webmaster Tools.

    Pass ``--allow-write`` (and have ``LV24_SEO_WRITE_SECRET`` set) to plan; apply with
    ``--allow-write --confirm TOKEN``. The plan reports the URL count, the 500-URL batch count
    and the remaining quota. ``--from-sitemap`` reads a urlset or a bounded sitemapindex and
    re-fetches it on apply; a changed URL set invalidates the plan token.
    """
    context: dict[str, Any] = {}

    def prepare() -> tuple[WritePlan, Callable[[WritePlan], Any] | None]:
        config = load_config(config_path)
        bing = config.providers.bing
        configured_site = bing.site.strip() if bing is not None else ""
        site_value = _bing_site_value(site, configured_site)
        host_value = _host_from_site(site_value)
        explicit_urls = _url_list(url or [], name="--url")
        explicit_on_host, explicit_off_host = _filter_host_urls(explicit_urls, host_value)
        use_sitemap = from_sitemap or bool(sitemap_url.strip())
        if not use_sitemap and not explicit_on_host:
            raise SeoError(
                code=INVALID_INPUT,
                message="at least one URL is required",
                next_action="pass --url https://example.com/page or --from-sitemap",
            )

        sitemap_target = ""
        sitemap_urls: list[str] = []
        sitemap_errors: list[str] = []
        sitemap_truncated = False
        off_host = explicit_off_host
        if use_sitemap:
            sitemap_target = _indexnow_sitemap_url(sitemap_url, config)
            inventory = _read_sitemap_inventory(sitemap_target, host_value)
            sitemap_errors = inventory.errors
            sitemap_truncated = inventory.truncated
            sitemap_urls, sitemap_off_host = _filter_host_urls(inventory.urls, host_value)
            off_host += sitemap_off_host
            if not sitemap_urls and not explicit_on_host:
                if sitemap_errors:
                    raise SeoError(
                        code=UPSTREAM_UNAVAILABLE,
                        message=(
                            "could not read the sitemap: " + ", ".join(sorted(set(sitemap_errors)))
                        ),
                        next_action=(
                            "check the sitemap URL, or pass explicit --url values instead"
                        ),
                        retryable=True,
                    )
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"the sitemap has no URLs for {host_value}",
                    next_action="check the sitemap or pass explicit --url values",
                )

        discovered = _dedupe([*explicit_on_host, *sitemap_urls])
        total = len(discovered)
        targets = discovered[:_INDEXNOW_URL_LIMIT]
        truncated = sitemap_truncated or len(discovered) > len(targets)
        limitations = _sitemap_write_limitations(
            action="bing",
            sitemap_target=sitemap_target,
            sitemap_errors=sitemap_errors,
            sitemap_truncated=sitemap_truncated,
            off_host=off_host,
            host=host_value,
            dropped=total - len(targets),
        )
        service = SeoService(config)
        quota_envelope = _service_write_result(service.bing_quota(site=site_value))
        quota_row = _bing_quota_row(quota_envelope)
        quota_meta = quota_envelope.get("meta")
        if isinstance(quota_meta, Mapping):
            limitations.extend(str(item) for item in quota_meta.get("limitations") or [])
        quota_limits = [
            int(value)
            for value in (quota_row.get("daily_remaining"), quota_row.get("monthly_remaining"))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if quota_limits:
            allowed = min(quota_limits)
            if allowed <= 0:
                raise SeoError(
                    code=QUOTA_EXCEEDED,
                    message="Bing reports no remaining URL submission quota",
                    next_action=(
                        "wait for the daily or monthly window to reset, or submit fewer URLs"
                    ),
                    retryable=False,
                )
            if len(targets) > allowed:
                targets = targets[:allowed]
                truncated = True
                limitations.append(
                    f"bing_quota_cap: {allowed} of {total} URL(s) fit the remaining quota"
                )
        context.update(
            {
                "truncated": truncated,
                "total": total,
                "limitations": _dedupe(limitations),
                "quota": quota_row,
            }
        )
        batches = _bing_batch_count(len(targets))
        plan = WritePlan(
            action="bing_submit",
            target=site_value,
            summary=(
                f"submit {len(targets)} URL(s) to Bing for {site_value} in {batches} batch(es)"
            ),
            payload={
                "site": site_value,
                "urls": targets,
                "batch_size": _BING_SUBMIT_BATCH,
                "batches": batches,
                **({"sitemap_url": sitemap_target} if sitemap_target else {}),
            },
        )

        def execute(_plan: WritePlan) -> Any:
            final_urls = targets
            if sitemap_target:
                current = _read_sitemap_inventory(sitemap_target, host_value)
                current_urls, _ = _filter_host_urls(current.urls, host_value)
                if current_urls != sitemap_urls:
                    raise SeoError(
                        code=CONFIRM_REQUIRED,
                        message="the sitemap changed since the dry run; the plan token is stale",
                        next_action=(
                            "rerun the dry run to review the new URL set, then apply the "
                            "fresh confirm_token"
                        ),
                        retryable=False,
                    )
                refreshed = _dedupe([*explicit_on_host, *current_urls])
                final_urls = refreshed[:_INDEXNOW_URL_LIMIT]
                context["truncated"] = sitemap_truncated or len(refreshed) > len(final_urls)
                context["total"] = len(refreshed)
                context["limitations"] = _sitemap_write_limitations(
                    action="bing",
                    sitemap_target=sitemap_target,
                    sitemap_errors=current.errors,
                    sitemap_truncated=current.truncated,
                    off_host=off_host,
                    host=host_value,
                    dropped=len(refreshed) - len(final_urls),
                )
            return service.bing_submit_urls(
                urls=final_urls,
                site=site_value,
                batch_size=_BING_SUBMIT_BATCH,
            )

        return plan, execute

    payload = _write_command(
        prepare,
        allow_write=allow_write,
        confirm=confirm,
        config_path=config_path,
        context=context,
    )
    emit(payload, as_json=as_json, lines=_write_lines(payload))


@bing_app.command("sitemaps")
def bing_sitemaps_command(
    details: Annotated[
        str,
        typer.Option(
            "--details",
            help="Feed URL whose sitemap-index children should be listed (read-only).",
        ),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List Bing sitemap feeds or one sitemap index's children (read-only).

    Bing reports feed status only: per-URL errors and per-URL crawl/index status are not
    exposed by ``GetFeeds`` or ``GetFeedDetails``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_sitemaps(details=details),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_bing_sitemaps_lines(payload))


@bing_app.command("issues")
def bing_issues_command(
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List Bing crawl issues with decoded issue-bit labels (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.bing_issues(),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_bing_issues_lines(payload))


@bing_app.command("pages")
def bing_pages_command(
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List Bing page-level traffic statistics (read-only).

    ``GetPageStats`` returns weekly ``QueryStats`` rows whose ``Query`` field is the page URL.
    An empty result carries ``no_rows_for_window`` and is not a zero-impression result.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_pages(site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(
            payload,
            ("page", "clicks", "impressions", "click_position", "impression_position", "date"),
        ),
    )


@bing_app.command("traffic")
def bing_traffic_command(
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show daily Bing site clicks and impressions (read-only).

    ``GetRankAndTrafficStats`` covers every Bing vertical since 2023-03-24, not only web
    search. An empty result carries ``no_rows_for_window``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_traffic(site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(payload, ("date", "clicks", "impressions")),
    )


@bing_app.command("crawl")
def bing_crawl_command(
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show daily Bing crawl statistics for the last six months (read-only).

    ``GetCrawlStats`` reports response-code buckets and index/link totals. The provider caps
    the window at six months, so every result carries ``crawl_stats_six_month_window``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_crawl(site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(
            payload,
            (
                "date",
                "code_2xx",
                "code_301",
                "code_302",
                "code_4xx",
                "code_5xx",
                "crawl_errors",
                "in_index",
                "in_links",
            ),
        ),
    )


@bing_app.command("urlinfo")
def bing_urlinfo_command(
    url: Annotated[
        str,
        typer.Option(
            "--url", help="Absolute URL or domain:<host>, for example domain:example.com."
        ),
    ] = "",
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show Bing index details for one URL, directory or domain (read-only).

    ``GetUrlInfo`` maps ``DiscoveryDate`` / ``LastCrawledDate`` and turns the WCF
    ``Date(-62135596800000)`` sentinel into ``null``; a never-crawled row carries
    ``never_crawled``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_url_info(url, site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(
            payload,
            (
                "url",
                "http_status",
                "is_page",
                "last_crawled_date",
                "discovery_date",
                "total_child_url_count",
                "anchor_count",
                "document_size",
            ),
        ),
    )


@bing_app.command("children")
def bing_children_command(
    url: Annotated[
        str,
        typer.Option(
            "--url", help="Absolute URL or domain:<host>, for example domain:example.com."
        ),
    ] = "",
    page: Annotated[
        int,
        typer.Option("--page", help="Zero-based provider page (default 0)."),
    ] = 0,
    pages: Annotated[
        int,
        typer.Option("--pages", help="Pages to read, starting at --page (default 1, max 10)."),
    ] = 1,
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List a bounded walk of Bing directory children (read-only).

    ``GetChildrenUrlInfo`` is a read POST. A full final page means more children may exist,
    so the result is ``truncated`` with ``children_page_cap`` instead of looking complete.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_children(url, page=page, pages=pages, site=site),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_bing_children_lines(payload))


@bing_app.command("users")
def bing_users_command(
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List users and roles that can access the Bing site (read-only).

    ``GetSiteRoles`` is read-only. Delegated verification codes are secrets and are not
    echoed; the JSON role enum is exposed as both ``role`` and ``role_code``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_users(site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(
            payload,
            ("email", "role", "site", "verification_site", "expired", "date"),
        ),
    )


@bing_app.command("sites")
def bing_sites_command(
    site: Annotated[
        str,
        typer.Option("--site", help="Echo this configured site in the result (optional)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List every site the Bing API key can see (read-only, account-scoped).

    ``GetUserSites`` needs no ``siteUrl``, so this works before ``providers.bing.site`` is
    configured. Verification codes are secrets and are not echoed.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_sites(site=site),
        strict=strict,
    )
    emit(
        payload,
        as_json=as_json,
        lines=_bing_rows_lines(payload, ("url", "is_verified")),
    )


@bing_app.command("joins")
def bing_joins_command(
    query: Annotated[
        str,
        typer.Option("--query", help="Search query; lists the pages that received impressions."),
    ] = "",
    page: Annotated[
        str,
        typer.Option("--page", help="Page URL or domain:<host>; lists the queries it received."),
    ] = "",
    site: Annotated[
        str,
        typer.Option("--site", help="Bing site URL (default: providers.bing.site)."),
    ] = "",
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read Bing's query/page joins (read-only).

    ``--query`` alone lists pages for that query, ``--page`` alone lists queries for that
    page, and both select daily detail rows for the pair. Every row carries explicit
    ``query`` and ``page`` keys; ``mode`` says which join was used.
    """
    payload = _run(
        config_path,
        lambda service, config: service.bing_joins(query=query, page=page, site=site),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_bing_joins_lines(payload))


@bing_app.command("quota")
def bing_quota_command(
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show the remaining Bing URL submission quota (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.bing_quota(),
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_bing_quota_lines(payload))


# -- indexing automation ---------------------------------------------------------------------


@indexing_app.command("status")
def indexing_status_command(
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help="all, bing, yandex, gsc or indexnow (default all).",
        ),
    ] = "all",
    limit: Annotated[
        int,
        typer.Option("--limit", help="URL rows per provider (default 50, max 1000)."),
    ] = 50,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help=(
                "Read Bing and Yandex quotas now; without it stored quotas are used. "
                "GSC's sitemap aggregate is read when the GSC client is available."
            ),
        ),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show how many URLs are indexed, submitted, queued or still pending (read-only).

    The GSC sitemap ``contents[].indexed`` aggregate is read whenever the GSC client is
    available; it is deprecated by Google and reported separately from the per-URL ``indexed``
    count. ``--live`` additionally reads Bing and Yandex quotas. Bing, Yandex and IndexNow
    expose no per-URL index count, so their ``indexed_total`` stays null with
    ``no_per_url_index_count``.

    ``write_secret_present`` reports only whether ``LV24_SEO_WRITE_SECRET`` is set; the value
    is never read into the envelope. When it is false, ``--allow-write`` would fail
    ``WRITE_DISABLED``.
    """
    payload = _run(
        config_path,
        lambda service, config: service.indexing_status(provider=provider, limit=limit, live=live),
        persist=True,
    )
    emit(payload, as_json=as_json, lines=_indexing_status_lines(payload))


@indexing_app.command("run")
def indexing_run_command(
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help="all, bing, yandex, gsc or indexnow (default all).",
        ),
    ] = "all",
    from_sitemap: Annotated[
        bool,
        typer.Option("--from-sitemap", help="Read the URL queue from the project sitemap."),
    ] = False,
    sitemap_url: Annotated[
        str,
        typer.Option(
            "--sitemap-url",
            help=(
                "Absolute sitemap URL (default: <project.base_url>/sitemap.xml); "
                "implies --from-sitemap."
            ),
        ),
    ] = "",
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="URL to include; repeat the flag. Merged with the sitemap."),
    ] = None,
    max_submit: Annotated[
        int | None,
        typer.Option("--max-submit", help="Submission cap for this run (default: config)."),
    ] = None,
    max_inspect: Annotated[
        int | None,
        typer.Option("--max-inspect", help="Index-check cap for this run (default: config)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Resubmit even when last_submitted_at is newer than lastmod."),
    ] = False,
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help=(
                "Apply provider submissions. Requires [indexing] enabled=true and "
                "LV24_SEO_WRITE_SECRET; without it the command only checks and plans."
            ),
        ),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Check index status and plan/apply submissions within provider quotas.

    Checks are read-only and run in both modes. Without ``--allow-write`` nothing is sent.
    With it, the engine re-reads quotas, verifies the plan hash, consumes an internal confirm
    token and records one ``indexing_runs`` row per provider. A scheduled run uses
    ``--allow-write``; the host scheduler must provide ``LV24_SEO_WRITE_SECRET`` in its
    environment (systemd ``Environment=``/``EnvironmentFile=``, Windows user environment,
    cron export). The secret is never embedded in the generated command line.

    A URL whose ``last_submitted_at`` is newer than its sitemap ``lastmod`` is skipped unless
    ``--force`` is set. Empty quotas are reported as ``quota_exhausted`` with
    ``not_attempted``; the command never reports zero as "all indexed".
    """
    payload = _run(
        config_path,
        lambda service, config: service.indexing_run(
            provider=provider,
            from_sitemap=from_sitemap,
            sitemap_url=sitemap_url,
            urls=url or [],
            max_submit=max_submit,
            max_inspect=max_inspect,
            force=force,
            allow_write=allow_write,
        ),
        persist=True,
    )
    emit(payload, as_json=as_json, lines=_indexing_run_lines(payload))


@indexing_app.command("history")
def indexing_history_command(
    limit: Annotated[
        int,
        typer.Option("--limit", help="Runs to show, newest first (default 20, max 1000)."),
    ] = 20,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List recorded indexing runs, newest first (read-only)."""
    payload = _run(
        config_path,
        lambda service, config: service.indexing_history(limit=limit),
        persist=True,
    )
    emit(payload, as_json=as_json, lines=_indexing_history_lines(payload))


def _write_command(
    prepare: Callable[[], tuple[WritePlan, Callable[[WritePlan], Any] | None]],
    *,
    allow_write: bool,
    confirm: str | None,
    config_path: Path | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one gated write: plan with ``--allow-write``, apply with ``--confirm``."""

    def action() -> dict[str, Any]:
        plan, execute = prepare()
        # A dry run needs no ledger, and must not create a database as a side effect.
        store, ledger_warnings = (None, []) if confirm is None else _write_ledger(config_path)
        try:
            gate = WriteGate(
                allow_write=allow_write,
                secret=os.environ.get(WRITE_SECRET_ENV, ""),
                store=store,
            )
            if confirm is None:
                result = gate.plan(plan)
                return ok(
                    {
                        "plan": plan.to_dict(),
                        "plan_hash": result.detail.get("plan_hash", ""),
                        "confirm_token": result.confirm_token,
                        "status": result.status,
                    },
                    meta={"dry_run": True, "action": plan.action, "target": plan.target},
                    warnings=ledger_warnings,
                )

            result = gate.apply(plan, confirm, execute=execute)
            data: dict[str, Any] = {"plan": plan.to_dict(), "status": result.status}
            meta: dict[str, Any] = {
                "dry_run": False,
                "action": plan.action,
                "target": plan.target,
            }
            warnings = list(ledger_warnings)
            if result.status == "skipped":
                data["reason"] = result.reason or "duplicate"
            if "result" in result.detail:
                raw = result.detail["result"]
                if (
                    isinstance(raw, Mapping)
                    and raw.get("ok") is True
                    and isinstance(raw.get("data"), Mapping)
                ):
                    # A service write returns the standard envelope. Keep the CLI shape flat:
                    # its data becomes data.result and its meta/warnings join the envelope.
                    data["result"] = dict(raw["data"])
                    service_meta = raw.get("meta")
                    if isinstance(service_meta, Mapping):
                        meta.update({str(key): item for key, item in service_meta.items()})
                    service_warnings = raw.get("warnings")
                    if isinstance(service_warnings, Sequence) and not isinstance(
                        service_warnings, str
                    ):
                        warnings.extend(str(item) for item in service_warnings)
                else:
                    data["result"] = _json_result(raw)
            return ok(data, meta=meta, warnings=warnings)
        finally:
            if store is not None:
                store.close()

    payload = run_envelope(action)
    return _with_write_context(payload, context)


def _with_write_context(
    payload: dict[str, Any], context: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Attach plan-time coverage facts (truncation, totals, limitations) to a write envelope."""
    if not context or not payload.get("ok"):
        return payload
    meta = dict(payload.get("meta") or {})
    for key in ("truncated", "total", "limitations", "quota"):
        if key not in context:
            continue
        if key == "limitations":
            # The service may add its own limitations (for example the undocumented Bing
            # submit idempotency); keep them together with the plan-time coverage notes.
            existing = meta.get("limitations")
            merged = (
                [str(item) for item in existing]
                if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes))
                else []
            )
            merged.extend(str(item) for item in context[key])
            meta[key] = _dedupe(merged)
        else:
            meta[key] = context[key]
    payload["meta"] = meta
    return payload


def _write_ledger(config_path: Path | None = None) -> tuple[Store | None, list[str]]:
    """Open the shared idempotency ledger, or explain why replay protection is off.

    ``WriteGate`` can only recognise a replayed ``confirm_token`` when it has the SQLite
    ledger: without it the same token stays valid for its whole TTL and the provider write can
    happen twice. The ledger lives in the *selected* workspace data directory, so
    ``--config /srv/site/seo.toml`` opens ``/srv/site/data`` even when the shell is elsewhere;
    resolving it from the CWD silently disabled replay protection. A write command must still
    work (with an explicit warning) when the config is missing or invalid, because sitemap and
    IndexNow submissions need no config.
    """
    try:
        config = load_config(config_path)
    except SeoError:
        return None, [
            "write ledger: config could not be loaded; replay protection is off for this run"
        ]
    store = _store_for(config)
    if store is None:
        return None, [
            "write ledger: no data directory (run `lv24-seo init`); "
            "the confirm token can be replayed within its 5-minute lifetime"
        ]
    return store, []


def _service_write_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a failed service write envelope into an exception.

    The service returns envelopes for every public method, but ``WriteGate.apply`` treats any
    returned value as a completed write: it marks the ledger claim ``sent``. A failed envelope
    must therefore be raised so the gate releases the claim and the CLI exits non-zero.
    """
    if not result.get("ok", False):
        error = result.get("error")
        details = error if isinstance(error, Mapping) else {}
        next_action = details.get("next_action")
        detail = details.get("detail")
        raise SeoError(
            code=str(details.get("code") or INTERNAL),
            message=str(details.get("message") or "the provider write failed"),
            next_action=next_action if isinstance(next_action, str) else None,
            retryable=bool(details.get("retryable")),
            detail=dict(detail) if isinstance(detail, Mapping) else {},
        )
    return dict(result)


def _json_result(value: Any) -> Any:
    """Render an adapter result without assuming it is already JSON-safe.

    ``indexnow_submit`` returns a :class:`~lv24_seo.providers.base.ProviderResult`, not a dict,
    so the envelope must go through its ``to_dict`` before ``json.dumps`` sees it.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rendered: Any = to_dict()
        return rendered
    if isinstance(value, Mapping):
        return dict(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _absolute_url(value: str, *, name: str) -> str:
    url = value.strip()
    if not url:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must not be empty",
            next_action=f"pass an absolute http(s) URL to {name}",
        )
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an absolute http(s) URL",
            next_action=f"pass a URL such as https://example.com/page to {name}",
            detail={"value": url},
        )
    return url


def _absolute_urls(values: Sequence[str], *, name: str) -> list[str]:
    cleaned = _url_list(values, name=name)
    if not cleaned:
        raise SeoError(
            code=INVALID_INPUT,
            message="at least one URL is required",
            next_action=f"pass {name} at least once",
        )
    return cleaned


def _url_list(values: Sequence[str], *, name: str) -> list[str]:
    """Validate and deduplicate absolute URLs; an empty input stays empty."""
    cleaned: list[str] = []
    for value in values:
        url = _absolute_url(value, name=name)
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _bare_host(value: str) -> str:
    host = value.strip()
    if not host or "://" in host or "/" in host:
        raise SeoError(
            code=INVALID_INPUT,
            message="IndexNow host must be a bare hostname",
            next_action="drop the scheme and path, for example example.com",
            detail={"host": value},
        )
    return host


def _host_from_site(site: str) -> str:
    """Derive the IndexNow host from ``providers.bing.site`` (URL or bare hostname)."""
    value = site.strip()
    if not value:
        return ""
    if "://" in value:  # noqa: SIM108 - a ternary would exceed the 100-column limit
        host = (urlsplit(value).hostname or "").lower()
    else:
        host = value.rstrip("/").lower()
    if not host or "/" in host:
        raise SeoError(
            code=INVALID_INPUT,
            message="providers.bing.site does not contain a hostname",
            next_action="set providers.bing.site to https://example.com",
        )
    return _bare_host(host)


def _indexnow_key(value: str) -> str:
    return validate_indexnow_key(value)


def _indexnow_sitemap_url(value: str, config: SeoConfig) -> str:
    """Explicit ``--sitemap-url`` or ``<project.base_url>/sitemap.xml``."""
    explicit = value.strip()
    if explicit:
        return _absolute_url(explicit, name="--sitemap-url")
    base_url = config.project.base_url.strip().rstrip("/")
    if not base_url:
        raise SeoError(
            code=CONFIG_INVALID,
            message="project.base_url is not configured; the default sitemap URL is unknown",
            next_action="set project.base_url in seo.toml or pass --sitemap-url",
        )
    return _absolute_url(f"{base_url}/sitemap.xml", name="--sitemap-url")


@dataclass(slots=True)
class _SitemapInventory:
    """URLs and honest coverage facts from one bounded sitemap read."""

    urls: list[str]
    errors: list[str]
    truncated: bool


def _read_sitemap_inventory(sitemap_url: str, host: str) -> _SitemapInventory:
    """Fetch and parse a sitemap read-only: one root plus at most five child sitemaps.

    ``allow_url`` keeps the IndexNow host in scope even when the sitemap itself is served from
    a CDN, while the seed origin stays in scope so its child sitemaps can be fetched. The
    caller still filters page URLs to exactly ``host`` before submitting.
    """
    seed = urlsplit(sitemap_url)
    seed_origin = (seed.scheme.lower(), seed.netloc.lower())
    wanted = host.lower()

    def in_scope(url: str) -> bool:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() == wanted:
            return True
        return (parts.scheme.lower(), parts.netloc.lower()) == seed_origin

    fetcher = Fetcher()
    try:
        reader = SitemapReader(
            lambda url: fetcher.fetch(url, max_bytes=SITEMAP_MAX_BYTES),
            allow_url=in_scope,
            max_urls=_INDEXNOW_URL_LIMIT,
            max_depth=1,
            max_docs=_INDEXNOW_SITEMAP_DOCS,
        )
        doc = reader.read(sitemap_url)
    finally:
        fetcher.close()
    return _SitemapInventory(
        urls=list(doc.urls),
        errors=list(doc.errors),
        truncated=doc.truncated,
    )


def _filter_host_urls(urls: Sequence[str], host: str) -> tuple[list[str], int]:
    """Keep URLs on ``host``; IndexNow rejects a URL list that crosses hosts."""
    wanted = host.lower()
    kept: list[str] = []
    off_host = 0
    for url in urls:
        if (urlsplit(url).hostname or "").lower() != wanted:
            off_host += 1
            continue
        if url not in kept:
            kept.append(url)
    return kept, off_host


def _bing_site_value(explicit: str, configured: str) -> str:
    """Resolve ``--site`` or ``providers.bing.site`` and require an absolute URL."""
    value = explicit.strip() or configured.strip()
    if not value:
        raise SeoError(
            code=CONFIG_INVALID,
            message="Bing Webmaster site is not configured",
            next_action="set providers.bing.site in seo.toml or pass --site https://example.com",
        )
    return _absolute_url(value, name="--site" if explicit.strip() else "providers.bing.site")


def _bing_batch_count(count: int) -> int:
    return (count + _BING_SUBMIT_BATCH - 1) // _BING_SUBMIT_BATCH


def _bing_quota_row(envelope: Mapping[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    if isinstance(data, Mapping):
        quota = data.get("quota")
        if isinstance(quota, Mapping):
            return dict(quota)
    return {}


def _bing_rows_lines(
    payload: Mapping[str, Any],
    columns: Sequence[str],
) -> list[str]:
    """Render one Bing read table and state an empty window instead of showing zero."""
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines: list[str] = []
    site = data.get("site_url")
    if site:
        lines.append(f"site: {site}")
    lines.extend(_rows_table(data.get("rows"), columns))
    lines.extend(_bing_limitation_notes(payload))
    return lines


def _bing_children_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [
        f"site: {data.get('site_url') or '-'}",
        f"url: {data.get('url') or '-'}",
        (
            f"page: {data.get('page')} "
            f"(read {data.get('pages_read')} of {data.get('pages')} requested)"
        ),
    ]
    lines.extend(
        _rows_table(
            data.get("rows"),
            (
                "url",
                "http_status",
                "is_page",
                "last_crawled_date",
                "total_child_url_count",
                "anchor_count",
            ),
        )
    )
    lines.extend(_bing_limitation_notes(payload))
    return lines


def _bing_joins_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [
        f"site: {data.get('site_url') or '-'}",
        f"mode: {data.get('mode') or '-'}",
        f"query: {data.get('query') or '-'}",
        f"page: {data.get('page') or '-'}",
    ]
    lines.extend(
        _rows_table(
            data.get("rows"),
            (
                "query",
                "page",
                "clicks",
                "impressions",
                "click_position",
                "impression_position",
                "position",
                "date",
            ),
        )
    )
    lines.extend(_bing_limitation_notes(payload))
    return lines


def _bing_limitation_notes(payload: Mapping[str, Any]) -> list[str]:
    """Explain the two empty-data codes so ``rows: 0`` is not read as zero traffic."""
    meta = payload.get("meta") or {}
    limitations = meta.get("limitations") if isinstance(meta, Mapping) else None
    codes = {str(item) for item in limitations or []}
    notes: list[str] = []
    if "no_rows_for_window" in codes:
        notes.append("note: no rows for the reported window; this is not a zero-impression result")
    if "provider_returned_no_rows" in codes:
        notes.append("note: Bing returned no rows for this lookup")
    if "never_crawled" in codes:
        notes.append("note: Bing reports no crawl date for this URL (never crawled)")
    if "crawl_stats_six_month_window" in codes:
        notes.append("note: Bing crawl stats cover the last six months")
    if "children_page_cap" in codes:
        notes.append("note: stopped at the page cap; more children may exist")
    return notes


def _bing_sitemaps_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [f"site: {data.get('site_url') or '-'}"]
    lines.extend(
        _rows_table(
            data.get("rows"),
            ("url", "type", "status", "last_crawled", "url_count", "file_size"),
        )
    )
    lines.append(
        "note: Bing reports feed status only; per-URL errors and crawl/index status are not exposed"
    )
    return lines


def _bing_issues_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [f"site: {data.get('site_url') or '-'}"]
    lines.extend(_rows_table(data.get("rows"), ("url", "http_code", "issues", "in_links")))
    lines.append("note: Bing reports an issue bitmask, not a type, severity or detection date")
    return lines


def _bing_quota_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    quota = data.get("quota")
    visible: Mapping[str, Any] = quota if isinstance(quota, Mapping) else {}
    lines = [
        f"site: {data.get('site_url') or '-'}",
        f"daily remaining: {_cell(visible.get('daily_remaining')) or '-'}",
        f"monthly remaining: {_cell(visible.get('monthly_remaining')) or '-'}",
        "note: monthly remaining can bind before daily; the monthly reset is undocumented",
    ]
    return lines


def _indexing_status_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    providers = data.get("providers")
    lines = [f"write secret: {'present' if data.get('write_secret_present') else 'missing'}"]
    if not isinstance(providers, Mapping) or not providers:
        return [*lines, "providers: none"]
    for name in sorted(providers):
        entry = providers[name]
        if not isinstance(entry, Mapping):
            continue
        lines.append(
            f"{name}: urls={entry.get('urls', 0)} submitted={entry.get('submitted', 0)} "
            f"inspected={entry.get('inspected', 0)} indexed={entry.get('indexed', 0)} "
            f"crawled={entry.get('crawled', 0)} queued={entry.get('queued', 0)} "
            f"pending={entry.get('pending', 0)}"
        )
        indexed_total = entry.get("indexed_total")
        if indexed_total is not None:
            lines.append(f"  indexed_total: {indexed_total} (gsc sitemaps, deprecated)")
        else:
            lines.append("  indexed_total: unknown (no per-URL index count)")
        quota = entry.get("quota")
        if isinstance(quota, Mapping):
            lines.append(
                "  quota: remaining="
                f"{_cell(quota.get('remaining')) or 'unknown'} "
                f"reset={_cell(quota.get('reset')) or 'unknown'}"
            )
        limits = entry.get("limitations")
        if isinstance(limits, Sequence) and not isinstance(limits, (str, bytes)) and limits:
            lines.append(f"  limitations: {', '.join(str(item) for item in limits)}")
    return lines


def _indexing_run_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        return []
    queue = data.get("queue")
    queue_info: Mapping[str, Any] = queue if isinstance(queue, Mapping) else {}
    lines = [
        f"mode: {'dry run' if meta.get('dry_run') else 'apply'}",
        f"queue: total={queue_info.get('total', 0)} selected={queue_info.get('selected', 0)} "
        f"truncated={bool(queue_info.get('truncated'))}",
    ]
    providers = data.get("providers")
    if isinstance(providers, Mapping):
        for name in sorted(providers):
            entry = providers[name]
            if not isinstance(entry, Mapping):
                continue
            lines.append(
                f"{name}: planned={entry.get('planned', 0)} submitted={entry.get('submitted', 0)} "
                f"checked={entry.get('checked', 0)} skipped={entry.get('skipped', 0)} "
                f"not_attempted={entry.get('not_attempted', 0)} status={entry.get('status', '-')}"
            )
            limits = entry.get("limitations")
            if isinstance(limits, Sequence) and not isinstance(limits, (str, bytes)) and limits:
                lines.append(f"  limitations: {', '.join(str(item) for item in limits)}")
    limits = data.get("limitations")
    if isinstance(limits, Sequence) and not isinstance(limits, (str, bytes)) and limits:
        lines.append(f"limitations: {', '.join(str(item) for item in limits)}")
    return lines


def _indexing_history_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    runs = data.get("runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)) or not runs:
        return ["no indexing runs recorded"]
    lines: list[str] = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        lines.append(
            f"{run.get('started_at') or '-'} {run.get('provider') or '-'} "
            f"{run.get('status') or '-'} planned={run.get('planned', 0)} "
            f"submitted={run.get('submitted', 0)} checked={run.get('checked', 0)} "
            f"skipped={run.get('skipped', 0)}"
        )
    return lines


def _sitemap_write_limitations(
    *,
    action: str,
    sitemap_target: str,
    sitemap_errors: Sequence[str],
    sitemap_truncated: bool,
    off_host: int,
    host: str,
    dropped: int,
) -> list[str]:
    """Human-readable coverage notes for the plan and apply envelopes."""
    notes: list[str] = []
    if sitemap_target and sitemap_truncated:
        notes.append(
            f"sitemap: inventory truncated by the {_INDEXNOW_URL_LIMIT}-URL or "
            f"{_INDEXNOW_SITEMAP_DOCS - 1}-child cap"
        )
    if sitemap_target and sitemap_errors:
        notes.append("sitemap: " + ", ".join(sorted(set(sitemap_errors))))
    if off_host:
        notes.append(f"{action}: {off_host} URL(s) outside {host} were skipped")
    if dropped:
        notes.append(
            f"{action}: capped at {_INDEXNOW_URL_LIMIT} URLs; {dropped} were not submitted"
        )
    return notes


def _write_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    raw_plan = data.get("plan")
    visible_plan: Mapping[str, Any] = raw_plan if isinstance(raw_plan, Mapping) else {}
    lines = [
        f"action: {visible_plan.get('action') or '-'}",
        f"target: {visible_plan.get('target') or '-'}",
        f"plan: {visible_plan.get('summary') or '-'}",
        f"status: {data.get('status') or '-'}",
    ]
    meta = payload.get("meta") or {}
    if isinstance(meta, Mapping):
        quota = meta.get("quota")
        if isinstance(quota, Mapping):
            lines.append(
                "quota: "
                f"daily {_cell(quota.get('daily_remaining')) or '-'} / "
                f"monthly {_cell(quota.get('monthly_remaining')) or '-'} remaining"
            )
    result = data.get("result")
    if isinstance(result, Mapping):
        if result.get("accepted") is not None:
            lines.append(f"accepted: {_cell(result.get('accepted'))}")
        if result.get("batches") is not None:
            lines.append(f"batches: {_cell(result.get('batches'))}")
        quota_after = result.get("quota_after")
        if isinstance(quota_after, Mapping):
            lines.append(
                "quota after: "
                f"daily {_cell(quota_after.get('daily_remaining')) or '-'} / "
                f"monthly {_cell(quota_after.get('monthly_remaining')) or '-'} remaining"
            )
    token = data.get("confirm_token")
    if token:
        lines.extend(
            [
                "",
                f"confirm token: {token}",
                "apply: rerun with --allow-write --confirm <token> within 5 minutes",
            ]
        )
    reason = data.get("reason")
    if reason:
        lines.append(f"reason: {reason}")
    return lines


@app.command()
def crawl(
    start: Annotated[
        str | None, typer.Option("--start", help="Start URL (default: project base_url).")
    ] = None,
    max_urls: Annotated[int | None, typer.Option("--max-urls", min=1, help="URL budget.")] = None,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Crawl one site within the URL budget and store pages and issues."""
    payload = _run(
        config_path,
        lambda service, config: service.crawl_and_store(start_url=start, max_urls=max_urls),
        persist=True,
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_crawl_lines(payload))


# -- reports ---------------------------------------------------------------------------------


@report_app.command("weekly")
def report_weekly(
    days: Annotated[int, typer.Option("--days", min=1, help="Current period length in days.")] = 7,
    lag_days: Annotated[
        int, typer.Option("--lag-days", min=0, help="Provider data lag in days.")
    ] = 2,
    send: Annotated[
        bool, typer.Option("--send", help="Deliver through the configured channels.")
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Directory for report.json and report.html."),
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Build the weekly digest and optionally write it to disk."""
    payload = _run(
        config_path,
        lambda service, config: _weekly(service, config, days, lag_days, send, out),
        persist=True,
        strict=strict,
    )
    emit(payload, as_json=as_json, lines=_report_lines(payload))


@report_app.command("diff")
def report_diff(
    base: Annotated[
        str | None,
        typer.Option("--base", help="Older snapshot period START..END (default: second newest)."),
    ] = None,
    head: Annotated[
        str | None,
        typer.Option("--head", help="Newer snapshot period START..END (default: newest)."),
    ] = None,
    markdown: Annotated[
        bool,
        typer.Option("--markdown", help="Print only the Markdown diff (PR comment / CI summary)."),
    ] = False,
    strict: Annotated[bool, typer.Option("--strict", help=_STRICT_HELP)] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Diff two stored weekly report snapshots (read-only, no provider calls)."""
    payload = _run(
        config_path,
        lambda service, config: service.report_diff(base=base, head=head),
        persist=True,
        strict=strict,
    )
    if markdown and payload.get("ok") and not as_json:
        for warning in payload.get("warnings") or []:
            typer.echo(f"warning: {warning}", err=True)
        data = payload.get("data") or {}
        typer.echo(str(data.get("markdown", "")), nl=False)
        raise typer.Exit(code=envelope_exit_code(payload))
    emit(payload, as_json=as_json, lines=_diff_lines(payload))


def _diff_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return ["report diff: no data"]
    return str(data.get("markdown", "")).rstrip().splitlines()


def _weekly(
    service: SeoService,
    config: SeoConfig,
    days: int,
    lag_days: int,
    send: bool,
    out: Path | None,
) -> dict[str, Any]:
    payload = service.weekly_report(days=days, lag_days=lag_days, send=send)
    if out is not None and payload.get("ok"):
        payload.setdefault("meta", {})["artifacts"] = _write_report(out, payload, config)
    return payload


# -- core web vitals -------------------------------------------------------------------------


@cwv_app.command("collect")
def cwv_collect(
    url: Annotated[
        list[str],
        typer.Option("--url", help="Absolute page URL; repeat the flag for more pages."),
    ],
    device: Annotated[
        str,
        typer.Option("--device", help="phone, desktop, tablet or all."),
    ] = "phone",
    source: Annotated[
        str,
        typer.Option("--source", help="crux (field), psi (lab) or both."),
    ] = "crux",
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category",
            help=(
                "PSI Lighthouse category; repeat for more "
                "(default: performance and seo). Ignored for crux-only runs."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=50, help="Maximum URLs to query (1..50)."),
    ] = 10,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Collect URL+device CWV and persist CrUX field rows for `report weekly`."""
    payload = _run(
        config_path,
        lambda service, config: service.cwv_collect(
            urls=url, device=device, source=source, limit=limit, categories=category
        ),
        persist=True,
    )
    emit(payload, as_json=as_json, lines=_cwv_lines(payload))


@cwv_app.command("audits")
def cwv_audits(
    url: Annotated[
        str,
        typer.Option("--url", help="Absolute page URL for one Lighthouse run."),
    ],
    strategy: Annotated[
        str,
        typer.Option("--strategy", help="mobile or desktop."),
    ] = "mobile",
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category",
            help=(
                "Lighthouse category; repeat for more "
                "(default: performance and seo). Example: best-practices."
            ),
        ),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read Lighthouse audits (lab, read-only) for one URL.

    Returns bounded audit rows with savings and item counts. PSI works without a key.
    """
    payload = _run(
        config_path,
        lambda service, config: service.cwv_audits(url=url, strategy=strategy, categories=category),
    )
    emit(payload, as_json=as_json, lines=_cwv_audits_lines(payload))


@cwv_app.command("history")
def cwv_history(
    url: Annotated[
        str,
        typer.Option("--url", help="Absolute page URL (mutually exclusive with --origin)."),
    ] = "",
    origin: Annotated[
        str,
        typer.Option("--origin", help="Origin URL, for example https://example.com."),
    ] = "",
    device: Annotated[
        str,
        typer.Option("--device", help="phone, desktop or tablet; omit for all devices."),
    ] = "",
    weeks: Annotated[
        int,
        typer.Option("--weeks", min=1, max=40, help="Weekly collection periods to return (1..40)."),
    ] = 25,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Read weekly CrUX p75 history (read-only; nothing is persisted).

    The newest collection period is last. Missing p75 cells are reported as
    `crux_history_gaps`, never filled.
    """
    payload = _run(
        config_path,
        lambda service, config: service.cwv_history(
            url=url, origin=origin, device=device, weeks=weeks
        ),
    )
    emit(payload, as_json=as_json, lines=_cwv_history_lines(payload))


def _cwv_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        return ["cwv: no data"]
    written = int(meta.get("written") or 0)
    lines = [
        f"cwv: {written} field row(s) written for {data.get('date') or '-'}",
        f"source: {meta.get('source') or '-'} · devices: {', '.join(meta.get('devices') or [])}",
        f"urls: {meta.get('urls_selected') or 0}/{meta.get('urls_total') or 0}",
    ]
    limitations = meta.get("limitations") or []
    if limitations:
        lines.append("limitations: " + ", ".join(str(item) for item in limitations))
    return lines


def _cwv_audits_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        return ["cwv audits: no data"]
    audits = [row for row in (data.get("audits") or []) if isinstance(row, Mapping)]
    categories = [row for row in (data.get("categories") or []) if isinstance(row, Mapping)]
    lines = [
        f"psi audits: {len(audits)} audit(s) for {data.get('url') or '-'} "
        f"({meta.get('strategy') or '-'})"
    ]
    if categories:
        lines.append(
            "categories: "
            + ", ".join(
                f"{row.get('category')} {_score_text(row.get('score'))}" for row in categories
            )
        )
    savings = sorted(
        (
            row
            for row in audits
            if isinstance(row.get("overall_savings_ms"), (int, float))
            and not isinstance(row.get("overall_savings_ms"), bool)
        ),
        key=lambda row: (-float(row["overall_savings_ms"]), str(row.get("id") or "")),
    )[:5]
    for row in savings:
        lines.append(f"  {row.get('id')}: {row.get('overall_savings_ms')} ms")
    limitations = meta.get("limitations") or []
    if limitations:
        lines.append("limitations: " + ", ".join(str(item) for item in limitations))
    return lines


def _cwv_history_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        return ["cwv history: no data"]
    periods = [row for row in (data.get("periods") or []) if isinstance(row, Mapping)]
    lines = [
        f"crux history: {len(periods)} period(s) for {data.get('target') or '-'} "
        f"({data.get('device') or 'all'})"
    ]
    if periods:
        newest = periods[-1]
        metrics = [
            name for name in (data.get("metrics") or []) if isinstance(name, str) and name in newest
        ]
        values = ", ".join(f"{name} {newest[name]}" for name in metrics)
        lines.append(
            f"newest {newest.get('first_date') or '-'}..{newest.get('last_date') or '-'}: "
            f"{values or 'no values'}"
        )
    limitations = meta.get("limitations") or []
    if limitations:
        lines.append("limitations: " + ", ".join(str(item) for item in limitations))
    return lines


def _score_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):.2f}"


# -- automation ------------------------------------------------------------------------------


@schedule_app.command("install")
def schedule_install(
    kind: Annotated[str, typer.Option("--kind", help="weekly, daily or indexing.")] = "weekly",
    cron: Annotated[
        str | None, typer.Option("--cron", help="Override the default cron expression.")
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="auto, cron, systemd or windows (default auto).")
    ] = "auto",
    print_only: Annotated[
        bool, typer.Option("--print", help="Render only (the default); wins over --allow-write.")
    ] = False,
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help="Touch the host scheduler; without it the command only prints the plan.",
        ),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Plan the weekly/daily report job; install it only with --allow-write."""

    dry_run = print_only or not allow_write

    def action() -> dict[str, Any]:
        spec, name = _schedule_spec(kind, cron, config_path, target)
        data = install_schedule(spec, target=target, dry_run=dry_run)
        warnings = list(data.get("warnings") or [])
        if print_only and allow_write:
            warnings.append("--print wins over --allow-write: nothing was installed")
        return ok(
            data,
            meta={"kind": name, "target": data["target"], "allow_write": allow_write},
            warnings=warnings,
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_schedule_lines(payload))


@schedule_app.command("status")
def schedule_status_cmd(
    kind: Annotated[str, typer.Option("--kind", help="weekly, daily or indexing.")] = "weekly",
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression to compare against.")
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="auto, cron, systemd or windows (default auto).")
    ] = "auto",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Report whether the generated job exists and matches the current spec (read-only)."""

    def action() -> dict[str, Any]:
        spec, name = _schedule_spec(kind, cron, config_path, target)
        data = schedule_status(spec, target=target)
        warnings = list(data.get("warnings") or [])
        return ok(data, meta={"kind": name, "target": data["target"]}, warnings=warnings)

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_schedule_status_lines(payload))


@schedule_app.command("uninstall")
def schedule_uninstall_cmd(
    kind: Annotated[str, typer.Option("--kind", help="weekly, daily or indexing.")] = "weekly",
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression of the installed job.")
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="auto, cron, systemd or windows (default auto).")
    ] = "auto",
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help="Remove the job; without it the command only prints the plan.",
        ),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Plan removal of the job; remove it only with --allow-write (idempotent)."""

    def action() -> dict[str, Any]:
        spec, name = _schedule_spec(kind, cron, config_path, target)
        data = uninstall_schedule(spec, target=target, dry_run=not allow_write)
        warnings = list(data.get("warnings") or [])
        if not allow_write and data.get("plan"):
            warnings.append("nothing was removed; rerun with --allow-write to uninstall")
        return ok(
            data,
            meta={"kind": name, "target": data["target"], "allow_write": allow_write},
            warnings=warnings,
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_schedule_uninstall_lines(payload))


@schedule_app.command("print")
def schedule_print(
    cron: Annotated[
        str,
        typer.Option(
            "--cron",
            help="Cron expression, for example '0 6 * * 1' (default: from --kind).",
        ),
    ] = "",
    kind: Annotated[
        str,
        typer.Option("--kind", help="weekly, daily or indexing (optional)."),
    ] = "",
    target: Annotated[
        str, typer.Option("--target", help="cron, systemd or windows (default cron).")
    ] = "cron",
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Render a schedule artefact without touching the host."""

    def action() -> dict[str, Any]:
        if kind.strip():
            spec, name = _schedule_spec(kind, cron or None, config_path, target)
            return ok(
                install_schedule(spec, target=target, dry_run=True),
                meta={"target": target, "kind": name},
            )
        if not cron.strip():
            raise SeoError(
                code=INVALID_INPUT,
                message="pass --kind or --cron",
                next_action=(
                    "use --kind weekly, --kind daily, --kind indexing or --cron '0 6 * * 1'"
                ),
            )
        config = load_config(config_path)
        name = _kind_from_cron(cron)
        spec = ScheduleSpec(
            name=f"lv24-seo-{name}",
            cron=cron,
            command=_report_command(name, config_path, target=target),
            workdir=_schedule_workdir(config_path),
            timezone=config.report.timezone,
            description=f"lv24-seo {name} SEO report",
        )
        return ok(install_schedule(spec, target=target, dry_run=True), meta={"target": target})

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_schedule_lines(payload))


@app.command()
def install(
    client: Annotated[str, typer.Option("--client", help="claude, opencode, vscode or cursor.")],
    print_only: Annotated[
        bool, typer.Option("--print", help="Print the JSON document only.")
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Accepted for uniformity; host config does not use it."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Register the MCP server in a host client config (merge, never replace)."""
    del config_path

    def action() -> dict[str, Any]:
        name = _canonical_client(client)
        fragment = _client_document(name)
        path = _client_config_path(name)
        if print_only:
            return ok({"client": name, "path": str(path), "written": False, "config": fragment})
        merged = _merge_document(name, _read_json_object(path), fragment)
        _write_text(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
        return ok({"client": name, "path": str(path), "written": True, "config": merged})

    payload = run_envelope(action)
    if not as_json and payload.get("ok") and print_only:
        data = payload.get("data") or {}
        typer.echo(json.dumps(data.get("config") or {}, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)
    emit(payload, as_json=as_json, lines=_install_lines(payload))


@export_app.command("tools")
def export_tools(
    out: Annotated[
        Path,
        typer.Option("--out", help="Destination JSON file."),
    ] = Path("schemas") / "tools.json",
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Accepted for uniformity; the registry needs no config."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Write ``schemas/tools.json`` from the plain-data MCP registry."""
    del config_path

    def action() -> dict[str, Any]:
        document = tools_as_json()
        destination = Path(out).expanduser()
        _write_text(destination, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        tools = document.get("tools")
        count = len(tools) if isinstance(tools, list) else 0
        return ok(
            {"path": str(destination), "tools": count},
            meta={"schema_version": document.get("schema_version")},
        )

    payload = run_envelope(action)
    lines: list[str] = []
    if payload.get("ok"):
        data = payload.get("data") or {}
        lines = [f"wrote {data.get('tools')} tools to {data.get('path')}"]
    emit(payload, as_json=as_json, lines=lines)


@export_app.command("skills")
def export_skills(
    out: Annotated[
        Path,
        typer.Option("--out", help="Destination directory for the skill files."),
    ] = Path("skills"),
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Accepted for uniformity; skills need no config."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Copy the packaged agent skills (SKILL.md + manifest) to ``--out``."""
    del config_path

    def action() -> dict[str, Any]:
        import shutil as _shutil

        import lv24_seo

        source = _skills_source()
        destination = Path(out).expanduser()
        if source is None:
            raise SeoError(
                code=INTERNAL,
                message="packaged skills not found",
                next_action="reinstall lv24-seo from the wheel or copy skills/ from the repo",
                detail={"package": str(lv24_seo.__file__)},
            )
        destination.mkdir(parents=True, exist_ok=True)
        copied = 0
        for skill_dir in sorted(source.iterdir()):
            if not skill_dir.is_dir():
                continue
            target = destination / skill_dir.name
            if target.exists():
                _shutil.rmtree(target)
            _shutil.copytree(skill_dir, target)
            copied += 1
        manifest = source / "manifest.json"
        if manifest.is_file():
            _shutil.copy2(manifest, destination / "manifest.json")
        return ok({"path": str(destination), "skills": copied})

    payload = run_envelope(action)
    lines: list[str] = []
    if payload.get("ok"):
        data = payload.get("data") or {}
        lines = [f"wrote {data.get('skills')} skills to {data.get('path')}"]
    emit(payload, as_json=as_json, lines=lines)


def _skills_source() -> Path | None:
    """Locate the shipped skills: wheel data dir first, repo checkout second."""
    import lv24_seo

    package_dir = Path(str(lv24_seo.__file__)).parent
    for candidate in (
        package_dir.parent / "lv24_seo_skills",  # installed wheel layout
        package_dir.parent.parent / "skills",  # source checkout layout
    ):
        if candidate.is_dir():
            return candidate
    return None


# -- service wiring --------------------------------------------------------------------------


def _run(
    config_path: Path | None,
    operation: Callable[[SeoService, SeoConfig], dict[str, Any]],
    *,
    persist: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Load the workspace, run one service operation and close storage afterwards."""

    def action() -> dict[str, Any]:
        config = load_config(config_path)
        store = _store_for(config) if persist else None
        try:
            return operation(SeoService(config, store=store), config)
        finally:
            if store is not None:
                store.close()

    return _strict_payload(run_envelope(action), strict)


def _strict_payload(payload: dict[str, Any], strict: bool) -> dict[str, Any]:
    """Mark a payload as strict and make truncation visible through ``partial()``.

    ``meta.strict`` alone drives :func:`~lv24_seo.core.envelope.envelope_exit_code` (exit 5
    when the data is truncated). A truncated strict payload is also rebuilt through
    :func:`partial` so ``meta.limitations`` always explains what is missing, even for a
    provider that set ``truncated`` without a limitation note.
    """
    if not strict:
        return payload
    meta = dict(payload.get("meta") or {})
    meta["strict"] = True
    if payload.get("ok") and meta.get("truncated"):
        limitations = list(meta.get("limitations") or [])
        if not limitations:
            limitations = ["truncated: a provider or a local cap limited this response"]
        return partial(
            payload.get("data") or {},
            limitations=limitations,
            meta=meta,
            warnings=payload.get("warnings"),
        )
    return {**payload, "meta": meta}


def _store_for(config: SeoConfig) -> Store | None:
    """Open the SQLite store only when ``init`` created the data directory."""
    from lv24_seo.composition import open_store

    return open_store(config)


# -- init helpers ----------------------------------------------------------------------------


def _project_from_property(raw: str | None) -> dict[str, str]:
    value = (raw or "").strip()
    if not value:
        return {"name": "", "base_url": "", "default_property": ""}
    if value.startswith("sc-domain:"):
        host = value[len("sc-domain:") :].strip().strip("/").lower()
        return {
            "name": host,
            "base_url": f"https://{host}" if host else "",
            "default_property": f"sc-domain:{host}" if host else value,
        }
    if "://" in value:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        scheme = parts.scheme or "https"
        base_url = f"{scheme}://{host}" if host else value.rstrip("/")
        return {
            "name": host or value,
            "base_url": base_url,
            "default_property": f"sc-domain:{host}" if host else value,
        }
    host = value.split("/", 1)[0].lower()
    return {
        "name": host,
        "base_url": f"https://{host}",
        "default_property": f"sc-domain:{host}",
    }


def _yandex_host(base_url: str) -> str:
    parts = urlsplit(base_url)
    if not parts.hostname:
        return ""
    port = 443 if (parts.scheme or "https") == "https" else 80
    return f"{parts.scheme or 'https'}:{parts.hostname}:{port}"


def _render_template(*, project: Mapping[str, str], data_dir: str) -> str:
    base_url = project.get("base_url", "")
    psi_urls = [f"{base_url}/"] if base_url else []
    return _TEMPLATE.substitute(
        name=_toml_string(project.get("name", "")),
        base_url=_toml_string(base_url),
        default_property=_toml_string(project.get("default_property", "")),
        gsc_property=_toml_string(project.get("default_property", "")),
        yandex_host_id=_toml_string(_yandex_host(base_url)),
        bing_site=_toml_string(base_url),
        crux_origin=_toml_string(base_url),
        psi_urls=json.dumps(psi_urls, ensure_ascii=False),
        data_dir=_toml_string(data_dir),
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _display_path(path: Path) -> str:
    text = str(path).replace("\\", "/")
    if not path.is_absolute() and not text.startswith("."):
        return f"./{text}"
    return text


# -- report helpers --------------------------------------------------------------------------


def _write_report(
    out: Path,
    payload: Mapping[str, Any],
    config: SeoConfig,
) -> dict[str, str]:
    directory = Path(out).expanduser()
    if directory.exists() and not directory.is_dir():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{directory} is not a directory",
            next_action="pass a directory to --out",
        )
    _ensure_dir(directory)
    data = payload.get("data")
    json_path = directory / "report.json"
    html_path = directory / "report.html"
    _write_text(json_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    document = ""
    if isinstance(data, Mapping):
        try:
            report = _report_from_payload(data)
            document = render_html(report, locale=config.report.locale)
        except (AttributeError, KeyError, TypeError, ValueError):
            document = ""
    if not document:
        document = _fallback_html(data, locale=config.report.locale)
    _write_text(html_path, document)
    return {"json": str(json_path), "html": str(html_path)}


def _report_from_payload(data: Mapping[str, Any]) -> Report:
    if not any(key in data for key in ("headline", "sections", "period", "kind")):
        raise ValueError("payload is not a report document")

    period = data.get("period")
    if isinstance(period, Mapping):
        start = str(period.get("start") or "")
        end = str(period.get("end") or "")
    else:
        start = str(data.get("period_start") or "")
        end = str(data.get("period_end") or "")

    sections: list[ReportSection] = []
    raw_sections = data.get("sections")
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, Sequence) or isinstance(raw_sections, (str, bytes)):
        raise ValueError("report sections must be a list")
    for raw in raw_sections:
        if not isinstance(raw, Mapping):
            continue
        rows: list[dict[str, Any]] = []
        for row in raw.get("rows") or []:
            if isinstance(row, Mapping):
                rows.append(dict(row))
        notes = [str(note) for note in raw.get("notes") or []]
        total = raw.get("total")
        truncated = raw.get("truncated")
        data_age = raw.get("data_age")
        sections.append(
            ReportSection(
                key=str(raw.get("key") or ""),
                title=str(raw.get("title") or ""),
                summary=str(raw.get("summary") or ""),
                rows=rows,
                notes=notes,
                total=total if isinstance(total, int) and not isinstance(total, bool) else None,
                truncated=truncated if isinstance(truncated, bool) else None,
                data_age=data_age if isinstance(data_age, str) and data_age.strip() else None,
            )
        )

    return Report(
        kind=str(data.get("kind") or "weekly"),
        property_id=str(data.get("property_id") or ""),
        period_start=start,
        period_end=end,
        headline=str(data.get("headline") or ""),
        sections=sections,
        generated_at=str(data.get("generated_at") or ""),
        limitations=[str(item) for item in data.get("limitations") or []],
        warnings=[str(item) for item in data.get("warnings") or []],
    )


def _fallback_html(data: Any, *, locale: str = "en") -> str:
    rendered = html_module.escape(json.dumps(data, ensure_ascii=False, indent=2))
    language = html_module.escape(normalize_locale(locale), quote=True)
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{language}">\n'
        "<head><meta charset='utf-8'><title>SEO report</title></head>\n"
        "<body><h1>SEO report</h1><pre>"
        f"{rendered}"
        "</pre></body></html>\n"
    )


# -- MCP host install helpers ----------------------------------------------------------------


def _canonical_client(value: str) -> str:
    name = value.strip().lower()
    if name not in _MCP_CLIENTS:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown MCP client {value!r}",
            next_action="use one of: claude, opencode, vscode, cursor",
        )
    return name


def _client_document(client: str) -> dict[str, Any]:
    if client == "opencode":
        return {
            "mcp": {
                "lv24-seo": {
                    "type": "local",
                    "command": ["uvx", *_MCP_ARGS],
                }
            }
        }
    if client == "vscode":
        return {
            "servers": {
                "lv24-seo": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": list(_MCP_ARGS),
                }
            }
        }
    return {
        "mcpServers": {
            "lv24-seo": {
                "command": "uvx",
                "args": list(_MCP_ARGS),
            }
        }
    }


def _merge_document(
    client: str,
    existing: Mapping[str, Any],
    fragment: Mapping[str, Any],
) -> dict[str, Any]:
    key = {"opencode": "mcp", "vscode": "servers"}.get(client, "mcpServers")
    merged = dict(existing)
    current = merged.get(key)
    if current is not None and not isinstance(current, Mapping):
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"`{key}` in the host config must be a JSON object",
            next_action="fix or remove the host config key, then rerun the command",
        )
    servers = dict(current) if isinstance(current, Mapping) else {}
    servers["lv24-seo"] = dict(fragment[key]["lv24-seo"])
    merged[key] = servers
    return merged


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeoError(
            code=INTERNAL,
            message=f"cannot read {path}",
            next_action="check the file permissions",
        ) from exc
    if not raw.strip():
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"{path} is not valid JSON",
            next_action="fix or remove the file, then rerun the command",
        ) from exc
    if not isinstance(document, dict):
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"{path} must contain a JSON object",
            next_action="fix or remove the file, then rerun the command",
        )
    return document


def _client_config_path(client: str) -> Path:
    home = Path.home()
    family = _platform_family()
    if client == "claude":
        if family == "windows":
            return _appdata(home) / "Claude" / "claude_desktop_config.json"
        if family == "macos":
            return (
                home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            )
        return home / ".config" / "Claude" / "claude_desktop_config.json"
    if client == "opencode":
        return home / ".config" / "opencode" / "opencode.json"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if family == "windows":
        return _appdata(home) / "Code" / "User" / "mcp.json"
    if family == "macos":
        return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    return home / ".config" / "Code" / "User" / "mcp.json"


def _platform_family() -> str:
    """Classify the host without comparing ``sys.platform`` to literals (mypy narrows those)."""
    name = platform.system().lower()
    if name == "windows":
        return "windows"
    if name == "darwin":
        return "macos"
    return "linux"


def _appdata(home: Path) -> Path:
    configured = os.environ.get("APPDATA")
    return Path(configured) if configured else home / "AppData" / "Roaming"


# -- schedule helpers ------------------------------------------------------------------------


def _schedule_kind(value: str) -> str:
    kind = value.strip().lower()
    if kind not in _SCHEDULE_CRONS:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown schedule kind {value!r}",
            next_action="use --kind weekly, --kind daily or --kind indexing",
        )
    return kind


def _schedule_spec(
    kind: str,
    cron: str | None,
    config_path: Path | None,
    target: str,
) -> tuple[ScheduleSpec, str]:
    """Build the scheduled job spec shared by install, status, uninstall and print."""

    name = _schedule_kind(kind)
    config = load_config(config_path)
    # "auto" must keep platform detection in the command builder, so pass None there.
    command_target = None if target == "auto" else target
    if name == "indexing":
        command = _indexing_command(config_path, target=command_target)
        description = "lv24-seo indexing automation"
    else:
        command = _report_command(name, config_path, target=command_target)
        description = f"lv24-seo {name} SEO report"
    spec = ScheduleSpec(
        name=f"lv24-seo-{name}",
        cron=cron or _SCHEDULE_CRONS[name],
        command=command,
        workdir=_schedule_workdir(config_path),
        timezone=config.report.timezone,
        description=description,
    )
    return spec, name


def _kind_from_cron(cron: str) -> str:
    fields = cron.split()
    return "daily" if len(fields) == 5 and fields[4] == "*" else "weekly"


def _report_command(kind: str, config_path: Path | None, *, target: str | None = None) -> str:
    executable = shutil.which("lv24-seo") or "lv24-seo"
    # The scheduled shell may run with a different cwd and several configs can
    # share one folder, so the resolved absolute config path is embedded.
    resolved = resolve_config_path(config_path)
    arguments = [executable, "report", "weekly"]
    if kind == "daily":
        arguments.extend(["--days", "1", "--lag-days", "0"])
    arguments.append("--send")
    if resolved is not None:
        arguments.extend(["--config", str(resolved)])
    arguments.append("--json")
    windows = target == "windows" or (target is None and _platform_family() == "windows")
    return subprocess.list2cmdline(arguments) if windows else shlex.join(arguments)


def _indexing_command(config_path: Path | None, *, target: str | None = None) -> str:
    """Render the scheduled indexing run.

    ``--allow-write`` is embedded because installing the job is the automation opt-in. The
    write secret is **not** embedded: the host scheduler must provide
    ``LV24_SEO_WRITE_SECRET`` in its environment (systemd ``Environment=`` or
    ``EnvironmentFile=``, a Windows user environment variable, or an exported cron variable).
    """
    executable = shutil.which("lv24-seo") or "lv24-seo"
    resolved = resolve_config_path(config_path)
    arguments = [executable, "indexing", "run", "--allow-write"]
    if resolved is not None:
        arguments.extend(["--config", str(resolved)])
    arguments.append("--json")
    windows = target == "windows" or (target is None and _platform_family() == "windows")
    return subprocess.list2cmdline(arguments) if windows else shlex.join(arguments)


def _schedule_workdir(config_path: Path | None) -> str:
    resolved = resolve_config_path(config_path)
    return str(resolved.parent if resolved is not None else Path.cwd())


# -- text helpers ----------------------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise SeoError(
            code=INTERNAL,
            message=f"cannot write {path}",
            next_action="check the directory permissions and the destination path",
        ) from exc


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot create directory {path}",
            next_action="pass a writable --data-dir and --config path",
        ) from exc


def _is_writable(path: Path) -> bool:
    try:
        probe = path / ".lv24-seo-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_dir_scope_note(data_dir: Path, config_path: Path | None) -> str | None:
    """Explain a data directory that still depends on the working directory.

    A config file pins a relative ``data_dir`` to its own folder, so an absolute value means
    the scope is unambiguous. A relative one means the built-in default or
    ``LV24_SEO_DATA_DIR`` is in play: running the same config from two directories then reads
    two different databases. That is the historical behaviour, not a crash, so the check is
    reported without failing ``doctor``.
    """
    if data_dir.is_absolute():
        return None
    note = f"{data_dir} is relative, so it follows the working directory"
    if config_path is not None:
        note += f" instead of {config_path.parent}"
    return f"{note}; set an absolute [storage].data_dir to pin it"


def _checks(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    raw = meta.get("checks") if isinstance(meta, Mapping) else None
    if raw is None and isinstance(data, Mapping):
        raw = data.get("checks")
    return [check for check in raw or [] if isinstance(check, dict)]


def _doctor_lines(checks: Sequence[Mapping[str, Any]], healthy: bool) -> list[str]:
    lines = []
    for check in checks:
        mark = "OK  " if check.get("ok") else "FAIL"
        lines.append(f"[{mark}] {check.get('id')}: {check.get('detail')}")
    if not healthy:
        lines.append("")
        lines.append("next: fix the failing checks and rerun `lv24-seo doctor`")
    return lines


def _init_lines(payload: Mapping[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    if not payload.get("ok") or not isinstance(data, Mapping):
        return []
    return [
        f"config: {data.get('config_path')}",
        f"data: {data.get('data_dir')}",
        "next:",
        "  lv24-seo auth set --provider gsc --stdin",
        "  lv24-seo properties",
    ]


def _config_lines(payload: Mapping[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    if not payload.get("ok") or not isinstance(data, Mapping):
        return []
    lines = [
        f"config: {data.get('path') or 'defaults (no seo.toml found)'}",
        f"data_dir: {data.get('data_dir')}",
    ]
    config_data = data.get("config")
    if isinstance(config_data, Mapping):
        lines.extend(_flatten_lines(config_data))
    return lines


def _flatten_lines(value: Any, prefix: str = "") -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_flatten_lines(item, name))
        return lines
    if isinstance(value, list):
        if not value:
            return []
        return [f"{prefix} = {json.dumps(value, ensure_ascii=False)}"]
    if isinstance(value, str) and not value.strip():
        return []
    return [f"{prefix} = {value}"]


def _properties_lines(payload: Mapping[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    if not payload.get("ok") or not isinstance(data, Mapping):
        return []
    project = data.get("project") or {}
    lines = [
        f"project: {project.get('name') or '-'} <{project.get('base_url') or '-'}>",
    ]
    if project.get("default_property"):
        lines.append(f"default property: {project['default_property']}")
    entries = [entry for entry in data.get("providers") or [] if isinstance(entry, Mapping)]
    rows = [
        [
            str(entry.get("provider", "")),
            str(entry.get("target") or "-"),
            str(entry.get("token", "")),
            "yes" if entry.get("ready") else "no",
        ]
        for entry in entries
    ]
    lines.extend(_table(["provider", "target", "token", "ready"], rows))
    return lines


def _stats_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return ["stats: no data"]
    period = data.get("period")
    previous = data.get("previous_period")
    raw_coverage = data.get("coverage")
    coverage: Mapping[str, Any] = raw_coverage if isinstance(raw_coverage, Mapping) else {}
    lines = [
        f"period: {_date_range(period)} (previous {_date_range(previous)})",
        (
            f"coverage: {coverage.get('returned', 0)}/{coverage.get('configured', 0)} ok, "
            f"{coverage.get('failed', 0)} failed, {coverage.get('missing', 0)} missing, "
            f"complete={_yes_no(coverage.get('complete'))}"
        ),
        "",
    ]
    services = data.get("services")
    rows: list[list[str]] = []
    errors: list[str] = []
    if isinstance(services, Sequence) and not isinstance(services, (str, bytes)):
        for service in services:
            if not isinstance(service, Mapping):
                continue
            rows.append(
                [
                    str(service.get("provider") or "-"),
                    str(service.get("status") or "-"),
                    str(service.get("rows", 0)),
                    _stats_metric_text(service.get("metrics")),
                ]
            )
            error = service.get("error")
            if isinstance(error, Mapping):
                errors.append(
                    f"{service.get('provider') or '-'}: "
                    f"{error.get('code') or 'ERROR'}: {error.get('message') or ''}"
                )
    lines.extend(_table(["provider", "status", "rows", "metrics"], rows))
    lines.extend(errors)
    limitations = data.get("limitations")
    if isinstance(limitations, Sequence) and not isinstance(limitations, (str, bytes)):
        for limitation in limitations:
            lines.append(f"limitation: {limitation}")
    return lines


def _date_range(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "-"
    return f"{value.get('start') or '-'}..{value.get('end') or '-'}"


def _stats_metric_text(metrics: Any) -> str:
    if not isinstance(metrics, Mapping) or not metrics:
        return "-"
    return " ".join(f"{name}={_stats_value(str(name), value)}" for name, value in metrics.items())


def _stats_value(name: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if name == "ctr":
        return f"{value * 100:.2f}%"
    if name in {"performance", "seo", "cumulative_layout_shift"}:
        return f"{value:.3f}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _gsc_filter_args(
    values: Sequence[str] | None,
    search_appearance: str,
    branded: str,
) -> list[dict[str, str]]:
    """Parse ``--filter``/``--search-appearance``/``--branded`` into API filter objects."""
    filters: list[dict[str, str]] = []
    for raw in values or []:
        text = raw.strip()
        parts = text.split(":", 2)
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise SeoError(
                code=INVALID_INPUT,
                message=f"--filter expects dimension:operator:expression, got {raw!r}",
                next_action="pass --filter query:contains:coffee",
            )
        filters.append(
            {
                "dimension": parts[0].strip(),
                "operator": parts[1].strip(),
                "expression": parts[2].strip(),
            }
        )
    appearance = search_appearance.strip()
    if appearance:
        filters.append(
            {
                "dimension": "searchAppearance",
                "operator": "equals",
                "expression": appearance,
            }
        )
    brand = branded.strip()
    if brand:
        canonical = {
            "branded": "BRANDED",
            "non_branded": "NON_BRANDED",
            "non-branded": "NON_BRANDED",
            "nonbranded": "NON_BRANDED",
            "unbranded": "NON_BRANDED",
        }.get(brand.casefold())
        if canonical is None:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"--branded expects branded or non_branded, got {branded!r}",
                next_action="pass --branded branded or --branded non_branded",
            )
        filters.append(
            {
                "dimension": "brandedClassification",
                "operator": "equals",
                "expression": canonical,
            }
        )
    return filters


def _gsc_filter_list_args(values: Sequence[str] | None) -> list[dict[str, Any]]:
    """Parse ``--filter-list dimension:expr1,expr2`` into multi-value filter objects."""
    filter_lists: list[dict[str, Any]] = []
    for raw in values or []:
        dimension, separator, expressions = raw.strip().partition(":")
        items = [part.strip() for part in expressions.split(",") if part.strip()]
        if not separator or not dimension.strip() or not items:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"--filter-list expects dimension:expr1,expr2, got {raw!r}",
                next_action="pass --filter-list country:FRA,DEU",
            )
        filter_lists.append({"dimension": dimension.strip(), "expressions": items})
    return filter_lists


def _gsc_inspect_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    inspection = data.get("inspection")
    if not isinstance(inspection, Mapping):
        return ["gsc inspect: no inspection result"]
    lines = [
        f"url: {inspection.get('url') or '-'}",
        f"verdict: {inspection.get('verdict') or '-'}",
        f"coverage: {inspection.get('coverage_state') or '-'}",
        (
            f"indexing: {inspection.get('indexing_state') or '-'}  "
            f"robots.txt: {inspection.get('robots_txt_state') or '-'}  "
            f"fetch: {inspection.get('page_fetch_state') or '-'}"
        ),
        f"crawled as: {inspection.get('crawled_as') or '-'}",
        f"last crawl: {inspection.get('last_crawl_time') or '-'}",
        (
            f"canonical: google={inspection.get('google_canonical') or '-'}  "
            f"user={inspection.get('user_canonical') or '-'}"
        ),
    ]
    sitemaps = inspection.get("sitemap")
    referring = inspection.get("referring_urls")
    lines.append(f"sitemaps: {_count(sitemaps)}  referring URLs: {_count(referring)}")
    rich = inspection.get("rich_results")
    if isinstance(rich, Mapping):
        detected = rich.get("detected_items")
        lines.append(
            f"rich results: {rich.get('verdict') or '-'} ({_count(detected)} detected types)"
        )
        if isinstance(detected, list):
            for item in detected[:5]:
                if not isinstance(item, Mapping):
                    continue
                issues = item.get("items")
                issue_count = 0
                if isinstance(issues, list):
                    issue_count = sum(
                        _count(entry.get("issues"))
                        for entry in issues
                        if isinstance(entry, Mapping)
                    )
                lines.append(f"  {item.get('rich_result_type') or '-'}: {issue_count} issue(s)")
    link = inspection.get("inspection_result_link")
    if link:
        lines.append(f"details: {link}")
    limitations = (payload.get("meta") or {}).get("limitations")
    if limitations:
        lines.append("limitations: " + "; ".join(str(item) for item in limitations))
    return lines


def _gsc_sitemaps_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    rows = data.get("rows") if isinstance(data, Mapping) else []
    lines = [f"gsc sitemaps: {_count(rows)}"]
    lines.extend(
        _rows_table(
            rows,
            (
                "path",
                "type",
                "is_sitemaps_index",
                "last_submitted",
                "last_downloaded",
                "is_pending",
                "errors",
                "warnings",
            ),
        )
    )
    index_row = data.get("sitemap_index") if isinstance(data, Mapping) else None
    if isinstance(index_row, Mapping):
        lines.append(f"sitemap index: {index_row.get('path') or '-'}")
        contents = index_row.get("contents")
        if isinstance(contents, list) and contents:
            lines.extend(_rows_table(contents, ("type", "submitted", "indexed")))
        else:
            lines.append("contents: none reported (child sitemap URLs are not exposed)")
    limitations = (payload.get("meta") or {}).get("limitations")
    if limitations:
        lines.append("limitations: " + "; ".join(str(item) for item in limitations))
    return lines


def _gsc_sitemap_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    sitemap = data.get("sitemap") if isinstance(data, Mapping) else None
    if not isinstance(sitemap, Mapping):
        return ["gsc sitemap: no entry"]
    lines = [
        f"path: {sitemap.get('path') or '-'}",
        f"type: {sitemap.get('type') or '-'}  index: {_yes_no(sitemap.get('is_sitemaps_index'))}",
        (
            f"submitted: {sitemap.get('last_submitted') or '-'}  "
            f"downloaded: {sitemap.get('last_downloaded') or '-'}  "
            f"pending: {_yes_no(sitemap.get('is_pending'))}"
        ),
        f"errors: {sitemap.get('errors', 0)}  warnings: {sitemap.get('warnings', 0)}",
    ]
    contents = sitemap.get("contents")
    if isinstance(contents, list) and contents:
        lines.append("contents (indexed is deprecated):")
        lines.extend(_rows_table(contents, ("type", "submitted", "indexed")))
    else:
        lines.append("contents: none reported")
    limitations = (payload.get("meta") or {}).get("limitations")
    if limitations:
        lines.append("limitations: " + "; ".join(str(item) for item in limitations))
    return lines


def _gsc_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    rows = data.get("rows") if isinstance(data, Mapping) else []
    lines = [f"gsc: {_count(rows)} rows"]
    if meta.get("start") and meta.get("end"):
        lines.append(f"period: {meta.get('start')}..{meta.get('end')}")
    if meta.get("data_age_days") is not None:
        lines.append(f"data age: {meta.get('data_age_days')} days")
    lines.extend(_rows_table(rows, _GSC_COLUMNS))
    return lines


def _yandex_queries_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    rows = data.get("rows") if isinstance(data, Mapping) else []
    lines = [f"yandex: {_count(rows)} queries"]
    lines.extend(_rows_table(rows, _YANDEX_COLUMNS))
    return lines


def _metrika_lines(payload: Mapping[str, Any], title: str, columns: Sequence[str]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    rows = data.get("rows") if isinstance(data, Mapping) else []
    lines = [f"{title}: {_count(rows)} rows"]
    if isinstance(meta, Mapping):
        if meta.get("start") and meta.get("end"):
            lines.append(f"period: {meta.get('start')}..{meta.get('end')}")
        if meta.get("data_age_days") is not None:
            lines.append(f"data age: {meta.get('data_age_days')} days")
        if meta.get("organic_only") is True:
            lines.append("scope: organic only (ym:s:lastTrafficSource=='organic')")
        if meta.get("group"):
            lines.append(f"group: {meta.get('group')}")
        if meta.get("sampled") is True:
            share = meta.get("sample_share")
            suffix = f" (sample_share={share})" if share is not None else ""
            lines.append(f"sampled: yes, values are estimates{suffix}")
    lines.extend(_rows_table(rows, columns))
    limitations = meta.get("limitations") if isinstance(meta, Mapping) else None
    if isinstance(limitations, Sequence) and not isinstance(limitations, (str, bytes)):
        for limitation in limitations:
            lines.append(f"limitation: {limitation}")
    return lines


def _metrika_goal_columns(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Build the goal table columns from the goal ids the envelope reports."""
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    columns: list[str] = []
    if isinstance(meta, Mapping):
        dimensions = meta.get("dimensions")
        if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
            columns.extend(str(name).removeprefix("ym:s:") for name in dimensions)
    goals = data.get("goals") if isinstance(data, Mapping) else None
    if isinstance(goals, Sequence) and not isinstance(goals, (str, bytes)):
        for goal in goals:
            columns.extend(
                f"goal{goal}{suffix}" for suffix in ("reaches", "visits", "users", "conversionRate")
            )
    return tuple(columns)


def _yandex_indexing_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping):
        return ["yandex indexing: no data"]
    lines = [f"host: {data.get('host_id') or '-'}"]
    for key in ("summary", "history", "problems", "events"):
        lines.append(f"{key}: {_count(data.get(key))}")
    if meta.get("data_age_days") is not None:
        lines.append(f"data age: {meta.get('data_age_days')} days")
    limitations = meta.get("limitations")
    if limitations:
        lines.append("limitations: " + "; ".join(str(item) for item in limitations))
    lines.extend(_rows_table(data.get("summary"), ("search_urls", "urls", "indexed", "excluded")))
    return lines


def _yandex_samples_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping):
        return ["yandex samples: no data"]
    rows = data.get("rows")
    kind = str(data.get("kind") or "indexing")
    lines = [f"yandex samples ({kind}): {_count(rows)} rows"]
    if isinstance(meta, Mapping) and meta.get("total") is not None:
        lines.append(f"total: {meta.get('total')}  truncated: {_yes_no(meta.get('truncated'))}")
    preferred = (
        ("event", "event_date", "url", "title", "target_url")
        if kind == "events"
        else ("status", "http_code", "url", "access_date")
    )
    lines.extend(_rows_table(rows, preferred))
    return lines


def _yandex_sitemaps_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping):
        return ["yandex sitemaps: no data"]
    rows = data.get("rows")
    lines = [f"yandex sitemaps: {_count(rows)} rows"]
    if isinstance(meta, Mapping) and meta.get("total") is not None:
        lines.append(f"total: {meta.get('total')}  truncated: {_yes_no(meta.get('truncated'))}")
    lines.extend(
        _rows_table(
            rows,
            (
                "sitemap_id",
                "sitemap_url",
                "urls_count",
                "errors_count",
                "children_count",
                "sitemap_type",
                "last_access_date",
            ),
        )
    )
    return lines


def _yandex_history_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping):
        return ["yandex history: no data"]
    rows = data.get("rows")
    kind = str(data.get("kind") or "sqi")
    lines = [f"yandex history ({kind}): {_count(rows)} points"]
    if isinstance(meta, Mapping) and meta.get("truncated"):
        lines.append("truncated: yes")
    lines.extend(
        _rows_table(
            rows,
            (
                "date",
                "value",
                "appeared",
                "removed",
                "searchable",
                "excluded",
                "shows",
                "clicks",
                "position",
                "avg_click_position",
            ),
        )
    )
    return lines


def _yandex_queue_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    if not isinstance(data, Mapping):
        return ["yandex queue: no data"]
    rows = data.get("rows")
    lines = [f"yandex queue: {_count(rows)} tasks"]
    if isinstance(meta, Mapping) and meta.get("total") is not None:
        lines.append(f"total: {meta.get('total')}  truncated: {_yes_no(meta.get('truncated'))}")
    lines.extend(_rows_table(rows, ("task_id", "state", "url", "added_time")))
    return lines


def _crawl_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return ["crawl: no data"]
    summary = data.get("summary") or {}
    graph = data.get("graph") or {}
    lines = [
        f"crawl: {summary.get('start_url') or '-'}",
        (
            f"urls: {summary.get('urls', 0)}  fetched: {summary.get('fetched', 0)}  "
            f"errors: {summary.get('errors', 0)}  blocked: {summary.get('blocked', 0)}  "
            f"issues: {summary.get('issues', 0)}  truncated: {_yes_no(summary.get('truncated'))}"
        ),
        (
            f"graph: nodes {graph.get('nodes', 0)}, edges {graph.get('edges', 0)}, "
            f"max_depth {graph.get('max_depth', 0)}, orphans {graph.get('orphans', 0)}, "
            f"broken {graph.get('broken', 0)}"
        ),
    ]
    coverage = data.get("sitemap_coverage")
    if isinstance(coverage, Mapping):
        counts = coverage.get("counts") or {}
        if isinstance(counts, Mapping):
            parts = ", ".join(f"{name} {counts.get(name, 0)}" for name in sorted(counts))
            lines.append(
                f"sitemap coverage ({coverage.get('sitemap_urls', 0)} advertised, "
                f"complete: {_yes_no(coverage.get('complete'))}): {parts}"
            )
    issues = [issue for issue in data.get("issues") or [] if isinstance(issue, Mapping)]
    if issues:
        lines.append("issues:")
        rows = [
            [
                str(issue.get("severity", "")),
                str(issue.get("rule_id", "")),
                str(issue.get("url", "")),
                str(issue.get("detail", "")),
            ]
            for issue in issues[:_MAX_HUMAN_ROWS]
        ]
        lines.extend(_table(["severity", "rule", "url", "detail"], rows))
        if len(issues) > _MAX_HUMAN_ROWS:
            lines.append(f"... {len(issues) - _MAX_HUMAN_ROWS} more issues")
    return lines


def _report_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data")
    lines: list[str] = []
    if isinstance(data, Mapping):
        meta = payload.get("meta") or {}
        locale = str(meta.get("locale") or "en") if isinstance(meta, Mapping) else "en"
        try:
            lines = render_text(_report_from_payload(data), locale=locale).splitlines()
        except (AttributeError, KeyError, TypeError, ValueError):
            lines = []
    if not lines:
        meta = payload.get("meta") or {}
        lines = [f"report: {meta.get('period') or 'ok'}"]
    artifacts = (payload.get("meta") or {}).get("artifacts") or {}
    if artifacts:
        lines.append("")
        lines.append(f"wrote: {artifacts.get('json')}")
        lines.append(f"wrote: {artifacts.get('html')}")
    return lines


def _schedule_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [
        f"target: {data.get('target')}",
        f"dry_run: {_yes_no(data.get('dry_run'))}",
        f"installed: {_yes_no(data.get('installed'))}",
        f"command: {data.get('command')}",
    ]
    files = data.get("files") or {}
    if isinstance(files, Mapping):
        for name, content in files.items():
            lines.append(f"{name}:")
            lines.extend(str(content).rstrip("\n").splitlines())
    paths = data.get("paths") or {}
    if isinstance(paths, Mapping):
        for name, path in paths.items():
            lines.append(f"installed {name}: {path}")
    if data.get("dry_run"):
        lines.append("next: rerun with --allow-write to install")
    return lines


def _schedule_status_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [
        f"target: {data.get('target')}",
        f"job: {data.get('name')}",
        f"state: {data.get('state')}",
        f"matches: {_match_label(data.get('matches'))}",
    ]
    artifacts = data.get("artifacts") or []
    unknown = data.get("state") == "unknown"
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            path = artifact.get("path")
            if unknown:
                lines.append(f"{artifact.get('kind')}: unknown ({path})")
            elif artifact.get("exists"):
                match = "matches" if artifact.get("matches") else "differs"
                lines.append(f"{artifact.get('kind')}: present, {match} ({path})")
            else:
                lines.append(f"{artifact.get('kind')}: missing ({path})")
    return lines


def _schedule_uninstall_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [
        f"target: {data.get('target')}",
        f"job: {data.get('name')}",
        f"dry_run: {_yes_no(data.get('dry_run'))}",
        f"removed: {_yes_no(data.get('removed'))}",
    ]
    for item in data.get("removed_items") or []:
        lines.append(f"removed: {item}")
    for item in data.get("plan") or []:
        lines.append(f"plan: {item}")
    if data.get("dry_run") and data.get("plan"):
        lines.append("next: rerun with --allow-write to remove")
    return lines


def _match_label(value: Any) -> str:
    return "unknown" if value is None else _yes_no(value)


def _install_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    lines = [f"client: {data.get('client')}", f"path: {data.get('path')}"]
    if data.get("written"):
        lines.append("written: yes")
    return lines


def _rows_table(rows: Any, preferred: Sequence[str]) -> list[str]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    mappings = [row for row in rows if isinstance(row, Mapping)]
    if not mappings:
        return [f"rows: {len(rows)}"]
    columns = [name for name in preferred if any(name in row for row in mappings)]
    if not columns:
        columns = list(mappings[0])[:4]
    body = [[_cell(row.get(column)) for column in columns] for row in mappings[:_MAX_HUMAN_ROWS]]
    lines = _table(columns, body)
    if len(mappings) > _MAX_HUMAN_ROWS:
        lines.append(f"... {len(mappings) - _MAX_HUMAN_ROWS} more rows")
    return lines


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    lines = [render(headers), render(["-" * width for width in widths])]
    lines.extend(render(row) for row in rows)
    return lines


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value).replace("\r", " ").replace("\n", " ")
    if len(text) > _MAX_CELL:
        return text[: _MAX_CELL - 3] + "..."
    return text


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _count(value: Any) -> int:
    return len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else 0


mcp_app = typer.Typer(help="Model Context Protocol server.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    transport: Annotated[
        str, typer.Option("--transport", help="stdio (default) or http.")
    ] = "stdio",
    port: Annotated[int, typer.Option("--port", help="Port for the HTTP transport.")] = 8765,
) -> None:
    """Run the MCP server over stdio or streamable HTTP."""
    import asyncio
    import json as json_module

    from lv24_seo.mcp import serve

    try:
        asyncio.run(serve(transport=transport, port=port))
    except SeoError as exc:
        payload = fail(exc)
        typer.echo(json_module.dumps(payload, ensure_ascii=False))
        raise typer.Exit(code=envelope_exit_code(payload)) from exc


if __name__ == "__main__":  # pragma: no cover
    app()
