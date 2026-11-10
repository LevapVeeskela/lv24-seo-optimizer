"""Write gating: plan, confirm token and idempotent apply (ADR-0003).

Every provider call that changes state goes through this module:

1. **Plan** - :meth:`WriteGate.plan` renders what would happen and returns a short-lived
   ``confirm_token``. It refuses with ``WRITE_DISABLED`` unless the caller explicitly opted in
   with ``allow_write=True`` (CLI ``--allow-write``).
2. **Apply** - :meth:`WriteGate.apply` verifies the token against the *same* plan, claims an
   idempotency slot in the store ledger and only then runs the caller's ``execute`` callable.

The token is ``HMAC-SHA256(secret, "<action>|<target>|<plan_hash>|<expiry>")`` where ``expiry``
is a Unix timestamp embedded in the token as ``<expiry>:<digest>``. A changed plan, a wrong
secret, a malformed token or an expired token all fail with ``CONFIRM_REQUIRED``; the token is
never echoed in an error message.

The secret is read from ``LV24_SEO_WRITE_SECRET`` by the CLI and is never stored in a plan,
an envelope or an exception. An empty secret is ``WRITE_DISABLED`` rather than a weak default.

Nothing here talks to a provider: the CLI passes the real call as ``execute`` only when the
adapter actually implements the write. A write that is not wired yet must fail honestly instead
of pretending it happened.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lv24_seo.core.errors import CONFIRM_REQUIRED, INVALID_INPUT, WRITE_DISABLED, SeoError

__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "GATE_ACTIONS",
    "WRITE_SECRET_ENV",
    "GateResult",
    "WriteGate",
    "WritePlan",
    "make_token",
    "plan_hash",
    "verify_token",
]

WRITE_SECRET_ENV = "LV24_SEO_WRITE_SECRET"  # noqa: S105 - an env var name, not a secret
DEFAULT_TOKEN_TTL_SECONDS = 300
GATE_ACTIONS = frozenset({"sitemap_submit", "recrawl", "indexnow"})

_TOKEN_DIGEST_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class WritePlan:
    """One intended provider mutation, rendered before anything is sent.

    ``action`` is one of :data:`GATE_ACTIONS`; ``target`` is the property id, host id or
    hostname the write belongs to. ``summary`` is the human line shown in the dry run, and
    ``payload`` is the exact JSON-safe document an adapter needs to execute the call.
    """

    action: str  # "sitemap_submit" | "recrawl" | "indexnow"
    target: str  # property id / host
    summary: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe rendering used by the CLI and MCP surfaces."""
        return {
            "action": self.action,
            "target": self.target,
            "summary": self.summary,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of a dry run or an apply: ``dry_run``, ``applied`` or ``skipped``."""

    status: str  # "dry_run" | "applied" | "skipped"
    plan: WritePlan
    confirm_token: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe rendering; empty optional fields are omitted."""
        payload: dict[str, Any] = {"status": self.status, "plan": self.plan.to_dict()}
        if self.confirm_token:
            payload["confirm_token"] = self.confirm_token
        if self.reason:
            payload["reason"] = self.reason
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


def plan_hash(plan: WritePlan) -> str:
    """SHA-256 of the canonical JSON form of the whole plan.

    Canonical means sorted keys, compact separators and UTF-8, so two equal plans hash equally
    regardless of dictionary insertion order. ``summary`` is covered as well: changing the text a
    human confirmed is a changed plan.
    """
    return hashlib.sha256(_canonical_json(plan.to_dict()).encode("utf-8")).hexdigest()


def make_token(
    plan: WritePlan,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint ``<expiry>:<HMAC-SHA256>`` for *plan*; the token dies after ``ttl_seconds``."""
    key = _require_secret(secret)
    if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise SeoError(
            code=INVALID_INPUT,
            message="confirm token TTL must be a positive number of seconds",
            next_action="use the default 300 seconds",
        )
    current = time.time() if now is None else now
    expiry = int(current) + int(ttl_seconds)
    return f"{expiry}:{_signature(plan, key, expiry)}"


def verify_token(
    plan: WritePlan,
    token: str,
    secret: str,
    *,
    now: float | None = None,
) -> bool:
    """``True`` only for a well-formed, unexpired token that signs exactly this plan."""
    key = _require_secret(secret)
    cleaned = token.strip()
    expiry_raw, separator, digest = cleaned.rpartition(":")
    if not separator or not expiry_raw.isdigit():
        return False
    if len(digest) != _TOKEN_DIGEST_LENGTH or any(char not in _HEX_DIGITS for char in digest):
        return False
    expiry = int(expiry_raw)
    current = int(time.time() if now is None else now)
    if expiry <= current:
        return False
    return hmac.compare_digest(_signature(plan, key, expiry), digest)


class WriteGate:
    """Policy object shared by the CLI and MCP write tools.

    ``allow_write`` is the explicit opt-in; ``store`` is any object exposing
    ``record_send(kind, property_id, period, content_hash, channel, ...) -> bool`` (the SQLite
    :class:`~lv24_seo.store.repositories.Store` qualifies). A duplicate ledger claim returns
    ``skipped`` instead of calling the provider again.
    """

    def __init__(self, *, allow_write: bool, secret: str, store: object | None = None) -> None:
        self._allow_write = bool(allow_write)
        self._secret = secret if isinstance(secret, str) else ""
        self._store = store
        if self._allow_write and not self._secret.strip():
            raise _missing_secret()

    def __repr__(self) -> str:
        """Never render the secret or the store object."""
        return (
            f"WriteGate(allow_write={self._allow_write}, "
            f"store={'configured' if self._store is not None else 'none'})"
        )

    def plan(self, plan: WritePlan) -> GateResult:
        """Dry-run *plan* and return the token needed to apply it."""
        self._require_enabled()
        _validate_plan(plan)
        token = make_token(plan, self._secret)
        return GateResult(
            status="dry_run",
            plan=plan,
            confirm_token=token,
            detail={
                "plan_hash": plan_hash(plan),
                "ttl_seconds": DEFAULT_TOKEN_TTL_SECONDS,
            },
        )

    def apply(
        self,
        plan: WritePlan,
        confirm_token: str,
        *,
        idempotency_key: str | None = None,
        execute: Callable[[WritePlan], Any] | None = None,
    ) -> GateResult:
        """Verify the token, claim the ledger slot and run *execute* exactly once.

        A bad token raises ``CONFIRM_REQUIRED``. A repeated ``(action, target, idempotency key)``
        is ``skipped``. The provider call is made only after the ledger claim succeeded, so a
        duplicate can never reach the network.
        """
        self._require_enabled()
        _validate_plan(plan)
        if not verify_token(plan, confirm_token, self._secret):
            raise SeoError(
                code=CONFIRM_REQUIRED,
                message="confirm token is missing, expired or does not match this plan",
                next_action=(
                    "rerun the dry run, review the plan and pass the fresh confirm_token "
                    "within 5 minutes"
                ),
                retryable=False,
            )

        digest = plan_hash(plan)
        key = _idempotency_key(idempotency_key, digest)
        if self._store is not None and not self._claim(plan, digest, key):
            return GateResult(
                status="skipped",
                plan=plan,
                reason="duplicate",
                detail={"plan_hash": digest, "idempotency_key": key},
            )

        detail: dict[str, Any] = {}
        if execute is not None:
            detail["result"] = execute(plan)
        return GateResult(status="applied", plan=plan, detail=detail)

    def _require_enabled(self) -> None:
        if self._allow_write:
            return
        raise SeoError(
            code=WRITE_DISABLED,
            message="write operations are disabled by default",
            next_action=(
                "review the dry-run plan, then rerun with --allow-write "
                "(or allow_write=True over MCP)"
            ),
            retryable=False,
        )

    def _claim(self, plan: WritePlan, digest: str, key: str) -> bool:
        record_send = getattr(self._store, "record_send", None)
        if not callable(record_send):
            raise SeoError(
                code=INVALID_INPUT,
                message="write ledger does not implement record_send",
                next_action="pass a Store or omit the store when idempotency is not needed",
            )
        claimed: Any = record_send(
            kind=plan.action,
            property_id=plan.target,
            period=key,
            content_hash=digest,
            channel="write",
        )
        return bool(claimed)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _signature(plan: WritePlan, secret: str, expiry: int) -> str:
    message = f"{plan.action}|{plan.target}|{plan_hash(plan)}|{expiry}"
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _require_secret(secret: str) -> str:
    value = secret.strip() if isinstance(secret, str) else ""
    if not value:
        raise _missing_secret()
    return value


def _missing_secret() -> SeoError:
    return SeoError(
        code=WRITE_DISABLED,
        message=f"write secret is empty: set {WRITE_SECRET_ENV}",
        next_action=(
            f"set {WRITE_SECRET_ENV} to a long random value before using --allow-write; "
            "never pass it on the command line"
        ),
        retryable=False,
    )


def _validate_plan(plan: WritePlan) -> None:
    if plan.action not in GATE_ACTIONS:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown write action {plan.action!r}",
            next_action=f"use one of: {', '.join(sorted(GATE_ACTIONS))}",
        )
    if not isinstance(plan.target, str) or not plan.target.strip():
        raise SeoError(
            code=INVALID_INPUT,
            message="write plan target is empty",
            next_action="pass a property id, host id or hostname",
        )
    if not isinstance(plan.payload, Mapping):
        raise SeoError(
            code=INVALID_INPUT,
            message="write plan payload must be a JSON object",
            next_action="pass a dict payload",
        )


def _idempotency_key(value: str | None, digest: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return digest
