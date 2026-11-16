"""``lv24-seo tasks``: the local finding/task lifecycle (F2/F7).

A finding becomes a task, is triaged, fixed and verified; a later observation of the same
finding reopens a verified task, which is how a regression is proven without a cloud account.
The commands are thin wrappers over :class:`~lv24_seo.store.repositories.Store`:

* ``add`` is idempotent on ``(kind, property_id, url, rule_id)`` -- running the same crawl or
  decay scan twice never duplicates a task;
* ``update`` accepts only :data:`~lv24_seo.store.repositories.TASK_TRANSITIONS` moves;
* every command prints the standard ``{ok, data, meta, warnings}`` envelope and exits with a
  stable code (``2`` for validation/usage failures, ``0`` otherwise).

The sub-app is attached by :func:`register`; ``main.py`` stays the only place that composes
the root command tree. Nothing here reads a provider token, and ``--payload`` rejects
credential-shaped keys so evidence can never become a secret store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from lv24_seo.cli import emit, run_envelope
from lv24_seo.composition import open_store
from lv24_seo.core.config import SeoConfig, load_config
from lv24_seo.core.envelope import ok, partial
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.store import TASK_STATES, Store

__all__ = ["register", "tasks_app"]

tasks_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Track findings through open -> in progress -> verified.",
)

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000
# Credential fields end with the noun (``gsc_token``, ``smtp_password``, ``api_key``); a field
# such as ``tokens`` or ``token_count`` is ordinary evidence and must stay allowed.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(token|secret|password|api[_-]?key|authorization)$", re.IGNORECASE
)


def register(app: typer.Typer) -> None:
    """Attach the ``tasks`` sub-app to the root CLI (called once by the integrator)."""
    app.add_typer(tasks_app, name="tasks", help="Track findings and their fix lifecycle.")


def _run_with_store(
    config_path: Path | None,
    action: Callable[[Store, SeoConfig], dict[str, Any]],
) -> dict[str, Any]:
    """Load the workspace, open its store, run one action and close the store."""

    def operation() -> dict[str, Any]:
        config = load_config(config_path)
        store = open_store(config)
        if store is None:
            raise SeoError(
                code=CONFIG_INVALID,
                message=(
                    "tasks need the local history store, but the data directory does not exist"
                ),
                next_action="run `lv24-seo init` (or create storage.data_dir) and retry",
            )
        try:
            return action(store, config)
        finally:
            store.close()

    return run_envelope(operation)


def _property_id(config: SeoConfig, explicit: str | None) -> str:
    value = (explicit or config.project.default_property).strip()
    if not value:
        raise SeoError(
            code=INVALID_INPUT,
            message="a property id is required for a task",
            next_action="pass --property or set project.default_property in the config",
        )
    return value


def _reject_secret_keys(value: object, *, path: str = "payload") -> None:
    """Refuse credential-shaped keys anywhere in the payload (values are never echoed)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"{path}.{name} looks like a credential",
                    next_action="tasks store evidence, never tokens; remove the field",
                )
            _reject_secret_keys(item, path=f"{path}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, path=f"{path}[{index}]")


def _payload(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"--payload is not valid JSON: {exc.msg}",
            next_action="pass a JSON object, for example '{\"months_stale\":16}'",
        ) from exc
    if not isinstance(decoded, dict):
        raise SeoError(
            code=INVALID_INPUT,
            message="--payload must be a JSON object",
            next_action="wrap the evidence in { ... }",
        )
    _reject_secret_keys(decoded)
    return decoded


def _task_line(task: Mapping[str, Any]) -> str:
    return f"{task['id']:>4} {task['state']:<11} {task['kind']:<12} {task['rule_id']} {task['url']}"


def _lines(payload: Mapping[str, Any], *, verb: str) -> list[str]:
    data = payload.get("data") or {}
    if isinstance(data, Mapping) and isinstance(data.get("task"), Mapping):
        task = data["task"]
        outcome = data.get("outcome")
        if outcome == "existing":
            return [f"already tracked: {_task_line(task)}"]
        if outcome == "reopened":
            return [f"reopened task {_task_line(task)}"]
        return [f"{verb} task {_task_line(task)}"]
    tasks = data.get("tasks") if isinstance(data, Mapping) else None
    if not tasks:
        return ["no tasks"]
    lines = [_task_line(task) for task in tasks]
    meta = payload.get("meta") or {}
    if isinstance(meta, Mapping) and meta.get("truncated"):
        lines.append(f"note: showing {meta.get('count')} of {meta.get('total')} tasks")
    return lines


@tasks_app.command("list")
def tasks_list(
    property_id: Annotated[
        str | None, typer.Option("--property", help="Property id (default: project default).")
    ] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by task kind.")] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by state: open, in_progress, verified, dismissed."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=_MAX_LIMIT, help="Maximum rows to return.")
    ] = _DEFAULT_LIMIT,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List tracked tasks, newest observation first (bounded)."""

    def action(store: Store, config: SeoConfig) -> dict[str, Any]:
        pid = (property_id or config.project.default_property).strip() or None
        try:
            rows = store.list_tasks(property_id=pid, kind=kind, state=state, limit=limit)
            total = store.count_tasks(property_id=pid, kind=kind, state=state)
        except ValueError as exc:
            raise SeoError(
                code=INVALID_INPUT,
                message=str(exc),
                next_action=f"use one of: {', '.join(TASK_STATES)}",
            ) from exc
        meta: dict[str, Any] = {
            "limit": limit,
            "count": len(rows),
            "total": total,
            "data_age_days": 0,
        }
        if pid:
            meta["property_id"] = pid
        if kind:
            meta["kind"] = kind
        if state:
            meta["state"] = state
        meta["truncated"] = len(rows) < total
        data: dict[str, Any] = {"tasks": rows}
        if meta["truncated"]:
            return partial(
                data,
                limitations=[
                    f"task list capped at {limit} of {total}; raise --limit or narrow the filters"
                ],
                meta=meta,
            )
        return ok(data, meta=meta)

    payload = _run_with_store(config_path, action)
    emit(payload, as_json=as_json, lines=_lines(payload, verb="listed"))


@tasks_app.command("add")
def tasks_add(
    kind: Annotated[
        str, typer.Option("--kind", help="Task kind, for example decay, finding or crawl.")
    ],
    url: Annotated[str, typer.Option("--url", help="Affected URL (absolute or site-relative).")],
    rule: Annotated[str, typer.Option("--rule", help="Rule or finding id that produced the task.")],
    property_id: Annotated[
        str | None, typer.Option("--property", help="Property id (default: project default).")
    ] = None,
    payload_json: Annotated[
        str | None,
        typer.Option("--payload", help="Evidence as a JSON object (never a token)."),
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Create a task once; a repeated add returns the existing row."""

    def action(store: Store, config: SeoConfig) -> dict[str, Any]:
        pid = _property_id(config, property_id)
        try:
            outcome = store.upsert_task(kind, pid, url, rule, payload=_payload(payload_json))
        except ValueError as exc:
            raise SeoError(
                code=INVALID_INPUT,
                message=str(exc),
                next_action="pass a non-empty --kind/--url/--rule",
            ) from exc
        rows = store.list_tasks(property_id=pid, kind=kind, url=url, rule_id=rule, limit=1)
        if not rows:  # pragma: no cover - the row was just written in this transaction
            raise RuntimeError("task disappeared right after upsert")
        return ok(
            {"task": rows[0], "outcome": outcome},
            meta={"property_id": pid, "kind": kind},
        )

    payload = _run_with_store(config_path, action)
    emit(payload, as_json=as_json, lines=_lines(payload, verb="added"))


@tasks_app.command("update")
def tasks_update(
    task_id: Annotated[int, typer.Argument(help="Task id from `lv24-seo tasks list`.")],
    state: Annotated[
        str,
        typer.Option("--state", help="Target state: open, in_progress, verified or dismissed."),
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Move a task to a new state; invalid transitions are rejected."""

    def action(store: Store, config: SeoConfig) -> dict[str, Any]:
        del config
        try:
            task = store.update_task_state(task_id, state)
        except KeyError as exc:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"task {task_id} not found",
                next_action="run `lv24-seo tasks list --json` to see tracked tasks",
            ) from exc
        except ValueError as exc:
            raise SeoError(
                code=INVALID_INPUT,
                message=str(exc),
                next_action=f"use one of: {', '.join(TASK_STATES)}",
            ) from exc
        return ok(
            {"task": task},
            meta={
                "task_id": task_id,
                "state": task["state"],
                "property_id": task["property_id"],
            },
        )

    payload = _run_with_store(config_path, action)
    emit(payload, as_json=as_json, lines=_lines(payload, verb="updated"))
