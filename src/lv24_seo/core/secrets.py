"""Secret storage for provider tokens and delivery credentials.

Resolution order for every read:

1. environment — ``LV24_SEO_<NAME_UPPER>`` (highest priority, read-only);
2. OS keyring — where the optional ``keyring`` dependency is importable and a real
   backend is registered;
3. JSON file — ``<data_dir>/secrets.json`` (mode ``0600``) only when file storage is
   allowed (``allow_file=True`` or ``LV24_SEO_ALLOW_FILE_SECRETS`` in ``1/true/yes``).

Guarantees:

* Values never appear in :meth:`SecretStore.__repr__`, :class:`SecretStatus`,
  :class:`~lv24_seo.core.errors.SeoError` messages or logs.
* ``keyring`` is imported lazily: the base install works without the extra.
* ``set``/``delete`` on an environment-provided secret raise ``WRITE_DISABLED`` because
  the environment always wins and cannot be changed by this process.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import tempfile
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, WRITE_DISABLED, SeoError

__all__ = ["SECRET_NAMES", "SecretSource", "SecretStatus", "SecretStore"]

SecretSource = Literal["env", "keyring", "file", "missing"]

ENV_PREFIX = "LV24_SEO_"
ALLOW_FILE_ENV = "LV24_SEO_ALLOW_FILE_SECRETS"
DEFAULT_SERVICE = "lv24-seo"

SECRET_NAMES: tuple[str, ...] = (
    "gsc_token",
    "yandex_webmaster_token",
    "yandex_metrika_token",
    "bing_api_key",
    "crux_api_key",
    "psi_api_key",
    "smtp_password",
    "telegram_bot_token",
)

_SECRETS_FILENAME = "secrets.json"
_TRUTHY = frozenset({"1", "true", "yes"})


@dataclass(frozen=True, slots=True)
class SecretStatus:
    """Where a secret comes from and whether it is usable.

    Deliberately carries no value: this object is safe to print, serialise and log.
    """

    name: str
    present: bool
    source: SecretSource
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    """One normalised entry of the on-disk secrets file."""

    value: str
    expires_at: datetime | None = None


class _KeyringBackend(Protocol):
    """The slice of ``keyring`` this module relies on."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class SecretStore:
    """Resolve, persist and report secrets without ever echoing them.

    ``env`` is injectable on purpose: tests and MCP servers can pass a snapshot instead of
    reading the live process environment.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        data_dir: Path | None = None,
        allow_file: bool | None = None,
        service: str = DEFAULT_SERVICE,
    ) -> None:
        resolved_env: Mapping[str, str] = os.environ if env is None else env
        self._env = resolved_env
        self._service = service
        self._data_dir = _resolve_data_dir(data_dir, resolved_env)
        self._allow_file = (
            allow_file if allow_file is not None else _env_flag(resolved_env.get(ALLOW_FILE_ENV))
        )

    def __repr__(self) -> str:
        # Never include values, the environment snapshot or resolved paths.
        return f"SecretStore(service={self._service!r}, allow_file={self._allow_file!r})"

    @property
    def _secrets_path(self) -> Path:
        return self._data_dir / _SECRETS_FILENAME

    def get(self, name: str) -> str | None:
        """Return the resolved secret or ``None``; env wins over keyring wins over file."""
        self._require_name(name)
        env_value = _non_empty(self._env.get(_env_key(name)))
        if env_value is not None:
            return env_value
        keyring_value = self._keyring_value(name)
        if keyring_value is not None:
            return keyring_value
        if not self._allow_file:
            return None
        entry = self._file_entry(name)
        return None if entry is None else entry.value

    def set(self, name: str, value: str) -> None:
        """Persist a secret to the keyring, or to the 0600 file when file mode is allowed."""
        self._require_name(name)
        if not _is_non_empty_str(value):
            raise SeoError(
                code=INVALID_INPUT,
                message="secret value must be a non-empty string",
                next_action="pass the token or password exactly as the provider issued it",
            )
        env_key = _env_key(name)
        if _non_empty(self._env.get(env_key)) is not None:
            raise SeoError(
                code=WRITE_DISABLED,
                message=f"{env_key} is set in the environment, which is read-only and always wins",
                next_action=f"unset {env_key} in this shell, then store the value again",
            )

        backend = _keyring_backend()
        if backend is not None:
            try:
                backend.set_password(self._service, name, value)
            except Exception:
                # A locked or revoked keyring must not lose the write when file mode is on.
                if not self._allow_file:
                    raise SeoError(
                        code=WRITE_DISABLED,
                        message="the OS keyring rejected the write and file storage is disabled",
                        next_action=(
                            "unlock the OS keyring, or set LV24_SEO_ALLOW_FILE_SECRETS=1 "
                            "to allow a 0600 secrets file"
                        ),
                    ) from None
            else:
                return

        if not self._allow_file:
            raise SeoError(
                code=WRITE_DISABLED,
                message=(
                    "no writable secret backend: the OS keyring is unavailable and "
                    "file storage is disabled"
                ),
                next_action=(
                    "install the secrets extra (`uv sync --extra secrets`) or set "
                    "LV24_SEO_ALLOW_FILE_SECRETS=1"
                ),
            )
        self._write_entry(name, value)

    def delete(self, name: str) -> None:
        """Remove a stored secret. Environment-provided values cannot be removed."""
        self._require_name(name)
        env_key = _env_key(name)
        if _non_empty(self._env.get(env_key)) is not None:
            raise SeoError(
                code=WRITE_DISABLED,
                message=f"{env_key} is set in the environment and cannot be removed here",
                next_action=f"unset {env_key} in this shell to remove it",
            )

        backend = _keyring_backend()
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.delete_password(self._service, name)
        if self._allow_file:
            self._delete_file_entry(name)

    def status(self, names: Iterable[str] | None = None) -> list[SecretStatus]:
        """Report presence, source and expiry for the requested secrets (never values)."""
        selected = SECRET_NAMES if names is None else tuple(names)
        for name in selected:
            self._require_name(name)
        return [self._resolve_status(name) for name in selected]

    def _resolve_status(self, name: str) -> SecretStatus:
        if _non_empty(self._env.get(_env_key(name))) is not None:
            return SecretStatus(name=name, present=True, source="env")
        if self._keyring_value(name) is not None:
            return SecretStatus(name=name, present=True, source="keyring")
        if self._allow_file:
            entry = self._file_entry(name)
            if entry is not None:
                return SecretStatus(
                    name=name,
                    present=True,
                    source="file",
                    expires_at=entry.expires_at,
                )
        return SecretStatus(name=name, present=False, source="missing")

    def _keyring_value(self, name: str) -> str | None:
        backend = _keyring_backend()
        if backend is None:
            return None
        try:
            raw = backend.get_password(self._service, name)
        except Exception:
            # A broken backend degrades to the next source instead of failing the command.
            return None
        return raw if _is_non_empty_str(raw) else None

    def _file_entry(self, name: str) -> _Entry | None:
        return _normalise_entry(self._read_document().get(name))

    def _write_entry(self, name: str, value: str) -> None:
        document = self._read_document()
        document[name] = {"value": value, "expires_at": None}
        self._write_document(document)

    def _delete_file_entry(self, name: str) -> bool:
        document = self._read_document()
        if name not in document:
            return False
        document.pop(name)
        self._write_document(document)
        return True

    def _read_document(self) -> dict[str, Any]:
        path = self._secrets_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            raise SeoError(
                code=CONFIG_INVALID,
                message=f"cannot read the secrets file at {path}",
                next_action="check the file permissions or fix LV24_SEO_DATA_DIR",
            ) from None
        if not raw.strip():
            return {}
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            raise SeoError(
                code=CONFIG_INVALID,
                message=f"the secrets file at {path} is not valid JSON",
                next_action="delete or fix the file; it must be a flat JSON object",
            ) from None
        if not isinstance(parsed, dict):
            raise SeoError(
                code=CONFIG_INVALID,
                message=f"the secrets file at {path} is not a flat JSON object",
                next_action="delete or fix the file; it must be a flat JSON object",
            )
        return {key: value for key, value in parsed.items() if isinstance(key, str)}

    def _write_document(self, document: Mapping[str, Any]) -> None:
        path = self._secrets_path
        payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=str(path.parent),
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            if os.name == "posix":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except OSError:
            raise SeoError(
                code=WRITE_DISABLED,
                message=f"cannot write the secrets file at {path}",
                next_action="check the directory permissions or configure the OS keyring",
            ) from None
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)

        if os.name != "posix":
            warnings.warn(
                f"secrets file {path} relies on platform ACLs: mode 0600 is not "
                f"enforced on {os.name!r}",
                UserWarning,
                stacklevel=2,
            )

    @staticmethod
    def _require_name(name: str) -> None:
        if name not in SECRET_NAMES:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"unknown secret name: {name!r}",
                next_action=f"use one of: {', '.join(SECRET_NAMES)}",
            )


def _env_key(name: str) -> str:
    return f"{ENV_PREFIX}{name.upper()}"


def _non_empty(value: str | None) -> str | None:
    """Treat blank environment values as unset while returning the value untouched."""
    if value is None or not value.strip():
        return None
    return value


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _env_flag(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _resolve_data_dir(data_dir: Path | None, env: Mapping[str, str]) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    configured = env.get("LV24_SEO_DATA_DIR")
    if configured is not None and configured.strip():
        return Path(configured).expanduser()
    try:
        from platformdirs import user_data_dir
    except Exception:
        return Path.home() / ".lv24-seo"
    return Path(user_data_dir("lv24-seo", "LV24"))


def _normalise_entry(item: object) -> _Entry | None:
    """Accept the structured form and the legacy flat string form."""
    if isinstance(item, str):
        return _Entry(value=item, expires_at=None) if item.strip() else None
    if isinstance(item, Mapping):
        raw_value = item.get("value")
        if not _is_non_empty_str(raw_value):
            return None
        return _Entry(value=cast(str, raw_value), expires_at=_parse_expiry(item.get("expires_at")))
    return None


def _parse_expiry(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _keyring_backend() -> _KeyringBackend | None:
    """Return a usable keyring backend, or ``None`` when the optional extra is absent."""
    try:
        # Imported by name so strict type-checking works with and without the extra.
        keyring_module = cast(Any, importlib.import_module("keyring"))
    except Exception:
        return None
    try:
        backend: object = keyring_module.get_keyring()
    except Exception:
        return None
    if not _backend_is_usable(backend):
        return None
    return cast(_KeyringBackend, backend)


def _backend_is_usable(backend: object) -> bool:
    """Reject keyring's fail/null placeholders, including one nested in a chainer."""
    if type(backend).__module__.startswith("keyring.backends.fail"):
        return False
    nested = getattr(backend, "backends", None)
    if nested is None:
        return True
    try:
        children = list(nested)
    except TypeError:
        return True
    return any(_backend_is_usable(child) for child in children)
