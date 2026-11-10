"""Structured logging with hard redaction.

Every record that leaves this module is scrubbed: the message, its ``%``-arguments, ``extra``
fields and the rendered line. Redaction is defence in depth, not a licence to log secrets: call
sites must still avoid passing raw credentials.

Policy
------
* Keys matching :data:`SENSITIVE_KEY_PATTERN` are always masked.
* Values carrying a known token prefix (``y0_``, ``eyJ``, ``ghp_``, ``sk-``) are masked under any
  key.
* Opaque runs of 32+ base64/hex characters are masked under any key.
* Values of 12+ characters keep two leading and two trailing characters (``ab***yz``); shorter
  values collapse to ``***``.
* Containers nested six or more levels deep are replaced with ``***``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

__all__ = [
    "SENSITIVE_KEY_PATTERN",
    "JsonFormatter",
    "RedactionFilter",
    "get_logger",
    "redact_mapping",
    "redact_value",
    "setup_logging",
]

_MASK = "***"
_MIN_LONG_VALUE = 12
_MAX_DEPTH = 6

SENSITIVE_KEY_PATTERN: re.Pattern[str] = re.compile(
    r"token|secret|password|authorization|cookie|api[_-]?key|private[_-]?key",
    re.IGNORECASE,
)

# Known bearer/token prefixes. The lookbehind keeps words such as "risk-free" intact.
_TOKEN_RE: re.Pattern[str] = re.compile(
    r"(?<![A-Za-z0-9])(?:y0_|eyJ|ghp_|sk-)[A-Za-z0-9+/=._-]{3,}"
)
_OPAQUE_VALUE_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
# A quoted value counts as one unit; otherwise an optional auth scheme plus the token.
_VALUE_RE = r"""(?:"[^"]*"|'[^']*'|(?:(?:Bearer|Basic|Token|Digest)\s+)?\S+)"""
_KEY_VALUE_RE: re.Pattern[str] = re.compile(
    rf"""((?:{SENSITIVE_KEY_PATTERN.pattern})\s*["']?\s*[:=]\s*){_VALUE_RE}""",
    re.IGNORECASE,
)

_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", logging.NOTSET, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


def redact_value(value: str) -> str:
    """Mask ``value`` while keeping a short hint of its shape.

    Values of 12+ characters keep two leading and two trailing characters (``ab***yz``);
    everything shorter collapses to ``***``. Already masked values are returned unchanged so
    redaction is safe to apply twice.
    """
    if _is_masked(value):
        return value
    if len(value) < _MIN_LONG_VALUE:
        return _MASK
    return f"{value[:2]}{_MASK}{value[-2:]}"


def redact_mapping(data: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Return a redacted copy of ``data`` without mutating it.

    ``depth`` is the nesting level of ``data`` itself; containers at six or more levels deep are
    replaced with ``***`` so a hostile or cyclic payload cannot exhaust the stack.
    """
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        name = key if isinstance(key, str) else str(key)
        redacted[name] = _redact_node(value, depth, mask_strings=_is_sensitive_key(name))
    return redacted


class RedactionFilter(logging.Filter):
    """Mask secrets in ``msg``, ``%``-arguments and ``extra`` fields of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_message(record.msg)
        record.args = _redact_arguments(record.args)
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS:
                continue
            record.__dict__[key] = _redact_node(value, 0, mask_strings=_is_sensitive_key(key))
        return True


class JsonFormatter(logging.Formatter):
    """One-line JSON records shaped as ``{"ts","level","logger","msg","extra"}``."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(self._payload(record), ensure_ascii=False, default=str)
        except Exception:
            return _fallback_line(record)

    def _payload(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": _format_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact_text(_safe_message(record)),
            "extra": _json_safe(_extra_fields(record)),
        }
        if record.exc_info:
            payload["exc"] = _redact_text(self.formatException(record.exc_info))
        return payload


def setup_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Install the redacting root handler.

    Idempotent: every call replaces the handlers currently attached to the root logger instead of
    stacking new ones. A closed ``stream`` is tolerated — logging must never break the caller.
    """
    resolved = _coerce_level(level)
    target = stream if stream is not None else sys.stderr
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = _SafeStreamHandler(target)
    handler.setLevel(resolved)
    handler.setFormatter(JsonFormatter() if json_output else _TextFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(resolved)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return the named logger; redaction is installed by :func:`setup_logging`."""
    return logging.getLogger(name)


class _SafeStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that swallows write/flush failures (closed stream, encoding, ...)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            return

    def handleError(self, record: logging.LogRecord) -> None:
        """Never print logging diagnostics: the caller's program keeps running."""


class _TextFormatter(logging.Formatter):
    """Human-readable fallback used when ``json_output=False``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        try:
            line = super().format(record)
            extras = _json_safe(_extra_fields(record))
        except Exception:
            return _MASK
        if extras:
            line = f"{line} {json.dumps(extras, ensure_ascii=False, default=str)}"
        return _redact_text(line)


def _is_sensitive_key(key: str) -> bool:
    return SENSITIVE_KEY_PATTERN.search(key) is not None


def _is_masked(value: str) -> bool:
    return value == _MASK or (len(value) == 7 and value[2:5] == _MASK)


def _mask_if_secret_like(value: str) -> str:
    masked = _TOKEN_RE.sub(lambda match: redact_value(match.group(0)), value)
    if _OPAQUE_VALUE_RE.fullmatch(masked):
        return redact_value(masked)
    return masked


def _redact_node(value: Any, depth: int, *, mask_strings: bool) -> Any:
    if isinstance(value, str):
        return redact_value(value) if mask_strings else _mask_if_secret_like(value)
    if isinstance(value, Mapping):
        if depth >= _MAX_DEPTH:
            return _MASK
        return {
            key if isinstance(key, str) else str(key): _redact_node(
                item,
                depth + 1,
                mask_strings=mask_strings or _is_sensitive_key(str(key)),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_DEPTH:
            return _MASK
        return [_redact_node(item, depth + 1, mask_strings=mask_strings) for item in value]
    return value


def _redact_message(message: Any) -> Any:
    if isinstance(message, str):
        return _mask_inline_tokens(message)
    if isinstance(message, Mapping):
        return redact_mapping(message)
    if isinstance(message, (list, tuple)):
        return [_redact_node(item, 0, mask_strings=False) for item in message]
    return message


def _redact_arguments(args: Any) -> Any:
    if isinstance(args, Mapping):
        return redact_mapping(args)
    if isinstance(args, tuple):
        return tuple(_redact_node(item, 0, mask_strings=False) for item in args)
    if args is None:
        return None
    return _redact_node(args, 0, mask_strings=False)


def _mask_inline_tokens(text: str) -> str:
    if not text:
        return text
    return _TOKEN_RE.sub(lambda match: redact_value(match.group(0)), text)


def _redact_text(text: str) -> str:
    """Scrub a rendered line: ``key=value`` pairs plus inline tokens and opaque runs."""
    if not text:
        return text
    cleaned = _KEY_VALUE_RE.sub(rf"\1{_MASK}", text)
    cleaned = _mask_inline_tokens(cleaned)
    return _OPAQUE_VALUE_RE.sub(lambda match: redact_value(match.group(0)), cleaned)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_RECORD_ATTRS:
            continue
        extras[key] = _redact_node(value, 0, mask_strings=_is_sensitive_key(key))
    return extras


def _safe_message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:
        return str(record.msg)


def _json_safe(value: Any) -> Any:
    """Convert ``value`` into something ``json.dumps`` accepts, stringifying the rest."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _format_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat(timespec="milliseconds")


def _fallback_line(record: logging.LogRecord) -> str:
    """Last-resort line for records with a broken ``__str__`` or a non-serialisable payload."""
    try:
        message = _redact_text(_safe_message(record))
    except Exception:
        message = _MASK
    try:
        timestamp = _format_timestamp(record.created)
    except Exception:
        timestamp = ""
    payload = {
        "ts": timestamp,
        "level": str(record.levelname),
        "logger": str(record.name),
        "msg": message,
        "extra": {},
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _coerce_level(level: str) -> int:
    try:
        resolved = logging.getLevelName(level.strip().upper())
    except Exception:
        return logging.INFO
    return resolved if isinstance(resolved, int) else logging.INFO
