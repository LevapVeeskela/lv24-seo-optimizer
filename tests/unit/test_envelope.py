"""Contract tests for the shared envelope and error codes."""

from __future__ import annotations

import pytest

from lv24_seo import __version__
from lv24_seo.core.envelope import envelope_exit_code, fail, ok, partial
from lv24_seo.core.errors import (
    AUTH_INVALID,
    CONFIG_INVALID,
    QUOTA_EXCEEDED,
    SeoError,
    exit_code_for,
)


def test_ok_envelope_shape() -> None:
    payload = ok({"x": 1}, meta={"duration_ms": 5}, warnings=["w"])
    assert payload == {
        "ok": True,
        "data": {"x": 1},
        "meta": {"version": __version__, "duration_ms": 5},
        "warnings": ["w"],
    }


def test_fail_envelope_hides_nothing_but_requires_code() -> None:
    payload = fail(SeoError(code=AUTH_INVALID, message="bad token", next_action="re-login"))
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": "AUTH_INVALID",
        "message": "bad token",
        "retryable": False,
        "next_action": "re-login",
    }
    assert payload["meta"] == {"version": __version__}


def test_partial_marks_limitations_and_is_ok() -> None:
    payload = partial({"rows": []}, limitations=["rows_cap_50000"], meta={"truncated": True})
    assert payload["ok"] is True
    assert payload["meta"]["limitations"] == ["rows_cap_50000"]
    assert payload["meta"]["truncated"] is True


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (AUTH_INVALID, 3),
        (QUOTA_EXCEEDED, 4),
        (CONFIG_INVALID, 2),
        ("INTERNAL", 1),
    ],
)
def test_exit_code_mapping(code: str, expected: int) -> None:
    assert exit_code_for(SeoError(code=code, message="x")) == expected


def test_envelope_exit_code_respects_strict_truncation() -> None:
    assert envelope_exit_code(ok()) == 0
    assert envelope_exit_code(ok(meta={"truncated": True})) == 0
    assert envelope_exit_code(ok(meta={"truncated": True, "strict": True})) == 5
    assert envelope_exit_code(fail(SeoError(code=QUOTA_EXCEEDED, message="x"))) == 4
