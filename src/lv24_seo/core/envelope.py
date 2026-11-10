"""The single response envelope used by CLI ``--json`` and MCP text output.

Shape::

    {"ok": true,  "data": {...}, "meta": {...}, "warnings": [...]}
    {"ok": false, "error": {...}, "meta": {...}, "warnings": [...]}

``meta`` carries at least ``version``; callers may add ``duration_ms``, ``truncated``,
``data_age_days`` and ``limitations``. Completeness flags belong in ``meta`` so agents can
check them without knowing a provider's payload shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lv24_seo.core.errors import SeoError, exit_code_for


def ok(
    data: Any = None,
    *,
    meta: Mapping[str, Any] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data if data is not None else {},
        "meta": dict(meta or {}),
        "warnings": list(warnings or []),
    }


def fail(
    error: SeoError,
    *,
    meta: Mapping[str, Any] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error.to_dict(),
        "meta": dict(meta or {}),
        "warnings": list(warnings or []),
    }


def partial(
    data: Any,
    *,
    limitations: Sequence[str],
    meta: Mapping[str, Any] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Successful-but-incomplete result: data is real, coverage is not total."""
    merged = dict(meta or {})
    merged.setdefault("truncated", False)
    merged["limitations"] = list(limitations)
    return ok(data, meta=merged, warnings=warnings)


def envelope_exit_code(payload: Mapping[str, Any]) -> int:
    """Exit code for a finished command given its envelope."""
    if payload.get("ok"):
        meta = payload.get("meta") or {}
        return 5 if meta.get("truncated") and meta.get("strict") else 0
    error = payload.get("error") or {}
    return exit_code_for(
        SeoError(
            code=str(error.get("code") or "INTERNAL"),
            message=str(error.get("message") or "unknown error"),
            next_action=error.get("next_action"),
            retryable=bool(error.get("retryable")),
        )
    )
