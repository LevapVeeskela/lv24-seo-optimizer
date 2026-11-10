"""``lv24-seo auth``: manual token entry, status and removal.

No network calls and no OAuth handshake live here. Wave 6 ships the manual paste flow
(``auth login`` prints instructions, ``auth set`` stores a token from stdin) because the
loopback/device flows need provider app registration that the owner has not provisioned yet.

Security rules:

* a secret is read from stdin (or a hidden prompt), never from argv and never echoed;
* ``auth status`` reads only :meth:`SecretStore.status`, which carries no values;
* the success payload repeats the provider, the stored flag and the source — nothing else.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from lv24_seo.cli import emit, run_envelope
from lv24_seo.core.config import SeoConfig, load_config
from lv24_seo.core.envelope import ok
from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.secrets import SecretStatus, SecretStore

auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Manage provider tokens (manual paste flow; values are never printed).",
)

_PROVIDER_SECRETS: dict[str, str] = {
    "gsc": "gsc_token",
    "yandex": "yandex_webmaster_token",
    "bing": "bing_api_key",
    "crux": "crux_api_key",
    "psi": "psi_api_key",
    "smtp": "smtp_password",
    "telegram": "telegram_bot_token",
}

_LOGIN_STEPS: dict[str, tuple[str, ...]] = {
    "gsc": (
        "Create an OAuth client (Desktop) or a service account in Google Cloud.",
        "Add the client or service-account email to the Search Console property as Full/Owner.",
        "Paste the access token when prompted by `lv24-seo auth set --provider gsc --stdin`.",
    ),
    "yandex": (
        "Create an app at https://oauth.yandex.ru/ with the webmaster:hostinfo scope.",
        "Add metrika:read to the same app if you also want Yandex Metrika data.",
        "Paste the issued token with `lv24-seo auth set --provider yandex --stdin`.",
        "Metrika needs its own token: LV24_SEO_YANDEX_METRIKA_TOKEN or a future provider entry.",
    ),
    "bing": (
        "Open Bing Webmaster Tools -> Settings -> API access and copy the API key.",
        "Store it with `lv24-seo auth set --provider bing --stdin`.",
    ),
    "crux": (
        "Enable the Chrome UX Report API in Google Cloud and create an API key.",
        "Store it with `lv24-seo auth set --provider crux --stdin`.",
    ),
    "psi": (
        "PageSpeed Insights works without a key (lower quota).",
        "For a higher quota create a PageSpeed Insights API key and store it with "
        "`lv24-seo auth set --provider psi --stdin`.",
    ),
}


@auth_app.command("status")
def auth_status(
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Show which providers have a token; values are never read back or printed."""

    def action() -> dict[str, Any]:
        config = load_config(config_path)
        store = SecretStore(data_dir=config.storage.data_dir)
        statuses = store.status(list(_PROVIDER_SECRETS.values()))
        by_name = {status.name: status for status in statuses}
        entries = [
            _status_entry(provider, secret, by_name.get(secret))
            for provider, secret in _PROVIDER_SECRETS.items()
        ]
        present = [entry["provider"] for entry in entries if entry["present"]]
        missing = [entry["provider"] for entry in entries if not entry["present"]]
        return ok({"providers": entries, "present": present, "missing": missing})

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_status_lines(payload))


@auth_app.command("set")
def auth_set(
    provider: Annotated[
        str, typer.Option("--provider", help="gsc|yandex|bing|crux|psi|smtp|telegram.")
    ],
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="Read the secret from stdin instead of a hidden prompt."),
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Store one secret from stdin. The value is never echoed or logged."""

    def action() -> dict[str, Any]:
        name = _canonical_provider(provider)
        secret = _PROVIDER_SECRETS[name]
        config = load_config(config_path)
        store = SecretStore(data_dir=config.storage.data_dir)
        value = _read_secret(stdin)
        store.set(secret, value)
        status = _first_status(store.status([secret]))
        source = status.source if status is not None else "unknown"
        return ok({"provider": name, "stored": True, "source": source})

    payload = run_envelope(action)
    if as_json:
        emit(payload, as_json=True)
    data = payload.get("data") or {}
    if payload.get("ok"):
        # The narrow machine-friendly confirmation is intentional: no token, no prefix.
        typer.echo(
            json.dumps(
                {
                    "provider": data.get("provider", ""),
                    "stored": True,
                    "source": data.get("source", "unknown"),
                },
                ensure_ascii=False,
            )
        )
        raise typer.Exit(code=0)
    emit(payload, as_json=False)


@auth_app.command("login")
def auth_login(
    provider: Annotated[str, typer.Option("--provider", help="gsc or yandex.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Print manual token instructions; nothing is written and no browser opens."""

    def action() -> dict[str, Any]:
        name = _canonical_provider(provider)
        if name not in _LOGIN_STEPS:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"provider {name!r} has no login flow; use `lv24-seo auth set`",
                next_action="use --provider gsc or --provider yandex",
            )
        config = load_config(config_path)
        return ok(
            {
                "provider": name,
                "flow": "manual",
                "target": _login_target(name, config),
                "steps": list(_LOGIN_STEPS[name]),
                "next": f"lv24-seo auth set --provider {name} --stdin",
            }
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_login_lines(payload))


@auth_app.command("logout")
def auth_logout(
    provider: Annotated[str, typer.Option("--provider", help="Provider name, see `auth status`.")],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Remove a stored secret. Environment-provided values stay read-only."""

    def action() -> dict[str, Any]:
        name = _canonical_provider(provider)
        secret = _PROVIDER_SECRETS[name]
        config = load_config(config_path)
        store = SecretStore(data_dir=config.storage.data_dir)
        store.delete(secret)
        status = _first_status(store.status([secret]))
        return ok(
            {
                "provider": name,
                "deleted": status is None or not status.present,
                "source": status.source if status is not None else "missing",
            }
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_logout_lines(payload))


def _canonical_provider(value: str) -> str:
    name = value.strip().lower()
    if name not in _PROVIDER_SECRETS:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown provider {value!r}",
            next_action=f"use one of: {', '.join(_PROVIDER_SECRETS)}",
        )
    return name


def _read_secret(stdin: bool) -> str:
    if stdin or not sys.stdin.isatty():
        raw = sys.stdin.readline()
    else:
        raw = typer.prompt("Secret", hide_input=True)
    value = raw.strip()
    if not value:
        raise SeoError(
            code=INVALID_INPUT,
            message="no secret was provided",
            next_action=(
                "pipe the value into the command, for example "
                "`lv24-seo auth set --provider gsc --stdin`"
            ),
        )
    return value


def _first_status(statuses: list[SecretStatus]) -> SecretStatus | None:
    return statuses[0] if statuses else None


def _status_entry(
    provider: str,
    secret: str,
    status: SecretStatus | None,
) -> dict[str, Any]:
    if status is None:
        return {
            "provider": provider,
            "secret": secret,
            "present": False,
            "source": "missing",
            "expires_at": None,
        }
    expires_at: str | None = None
    if isinstance(status.expires_at, datetime):
        expires_at = status.expires_at.isoformat()
    return {
        "provider": provider,
        "secret": secret,
        "present": status.present,
        "source": status.source,
        "expires_at": expires_at,
    }


def _login_target(provider: str, config: SeoConfig) -> str:
    providers = config.providers
    if provider == "gsc":
        gsc = providers.gsc
        return gsc.property if gsc is not None else ""
    webmaster = providers.yandex_webmaster
    return webmaster.host_id if webmaster is not None else ""


def _status_lines(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    entries = [entry for entry in data.get("providers") or [] if isinstance(entry, dict)]
    if not entries:
        return ["no providers configured"]
    rows = [
        [
            str(entry.get("provider", "")),
            str(entry.get("secret", "")),
            "present" if entry.get("present") else "missing",
            str(entry.get("source", "")),
        ]
        for entry in entries
    ]
    return _table(["provider", "secret", "state", "source"], rows)


def _login_lines(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    if not payload.get("ok"):
        return []
    lines = [f"manual login for {data.get('provider')} ({data.get('flow')})"]
    target = data.get("target")
    if target:
        lines.append(f"target: {target}")
    for index, step in enumerate(data.get("steps") or [], start=1):
        lines.append(f"{index}. {step}")
    lines.append(f"next: {data.get('next')}")
    return lines


def _logout_lines(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or {}
    if not payload.get("ok"):
        return []
    state = "removed" if data.get("deleted") else "still present"
    return [f"{data.get('provider')}: {state}", f"source: {data.get('source')}"]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    lines = [render(headers), render(["-" * width for width in widths])]
    lines.extend(render(row) for row in rows)
    return lines
