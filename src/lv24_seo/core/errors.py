"""Typed error model shared by providers, CLI and MCP.

Rules
-----
* Every failure that a user can act on is a :class:`SeoError` with a stable ``code``.
* ``code`` is part of the public contract (MCP ``isError`` payload, CLI JSON, docs).
* Nothing here ever carries a token or a raw response body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Stable machine codes. Extend carefully: agents and skills match on them.
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_INVALID = "AUTH_INVALID"
PROPERTY_NOT_FOUND = "PROPERTY_NOT_FOUND"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
TRUNCATED = "TRUNCATED"
WRITE_DISABLED = "WRITE_DISABLED"
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
INVALID_INPUT = "INVALID_INPUT"
CONFIG_INVALID = "CONFIG_INVALID"
INTERNAL = "INTERNAL"

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_QUOTA = 4
EXIT_PARTIAL = 5

_AUTH_CODES = frozenset({AUTH_REQUIRED, AUTH_INVALID})
_QUOTA_CODES = frozenset({QUOTA_EXCEEDED, UPSTREAM_UNAVAILABLE})


@dataclass(slots=True)
class SeoError(Exception):
    """A failure the caller can act on.

    ``message`` is safe to show to a human; ``next_action`` is a short imperative hint.
    ``detail`` must never contain secrets — providers are responsible for redaction.
    """

    code: str
    message: str
    next_action: str | None = None
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # dataclass(slots=True) rebuilds the class, so `super()` would bind the stale class.
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.next_action:
            payload["next_action"] = self.next_action
        if self.detail:
            payload["detail"] = self.detail
        return payload


def exit_code_for(error: SeoError) -> int:
    """Map an error to the documented CLI exit code."""
    if error.code in _AUTH_CODES:
        return EXIT_AUTH
    if error.code in _QUOTA_CODES:
        return EXIT_QUOTA
    if error.code in {INVALID_INPUT, CONFIG_INVALID}:
        return EXIT_USAGE
    if error.code == WRITE_DISABLED:
        return EXIT_USAGE
    if error.code == TRUNCATED:
        return EXIT_PARTIAL
    return EXIT_UNEXPECTED
