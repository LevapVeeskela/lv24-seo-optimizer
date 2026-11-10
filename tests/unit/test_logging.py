"""Contract tests for logging with hard redaction."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from lv24_seo.core.logging import (
    SENSITIVE_KEY_PATTERN,
    JsonFormatter,
    RedactionFilter,
    get_logger,
    redact_mapping,
    redact_value,
    setup_logging,
)

BUF = io.StringIO


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Keep the global root logger intact for the rest of the suite."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    propagate = root.propagate
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = propagate


def make_record(
    msg: Any = "message",
    args: tuple[Any, ...] | dict[str, Any] = (),
    **extra: Any,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="lv24_seo.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_sensitive_key_pattern_is_case_insensitive() -> None:
    for key in (
        "Authorization",
        "ACCESS_TOKEN",
        "client_secret",
        "X-Api-Key",
        "APIKEY",
        "private_key",
        "Cookie",
        "password",
    ):
        assert SENSITIVE_KEY_PATTERN.search(key), key
    assert SENSITIVE_KEY_PATTERN.search("user_id") is None


def test_redact_value_short_and_long() -> None:
    assert redact_value("") == "***"
    assert redact_value("short-token") == "***"
    assert redact_value("abcdefghijk") == "***"
    assert redact_value("abcdefghijyz") == "ab***yz"
    assert redact_value("ab***yz") == "ab***yz"


def test_redact_mapping_masks_sensitive_keys_and_keeps_scalars() -> None:
    data = {
        "Authorization": "Bearer abcdefghijklmnop",
        "client_secret": "s3cr3t",
        "nested": [{"api_key": "k" * 20, "title": "ok"}],
        "pairs": ("Bearer y0_AgAAAA", "plain"),
        "count": 3,
        "active": True,
        "missing": None,
    }

    redacted = redact_mapping(data)

    assert redacted["Authorization"] == "Be***op"
    assert redacted["client_secret"] == "***"
    assert redacted["nested"] == [{"api_key": "kk***kk", "title": "ok"}]
    assert redacted["pairs"] == ["Bearer ***", "plain"]
    assert redacted["count"] == 3
    assert redacted["active"] is True
    assert redacted["missing"] is None


def test_redact_mapping_does_not_mutate_input() -> None:
    data = {"token": "secret-value", "nested": {"password": "hunter2"}}

    redact_mapping(data)

    assert data == {"token": "secret-value", "nested": {"password": "hunter2"}}


def test_redact_mapping_masks_long_opaque_value_under_any_key() -> None:
    opaque = "QUJD" + "x" * 36

    redacted = redact_mapping({"blob": opaque, "note": "hello"})

    assert opaque not in redacted["blob"]
    assert "***" in redacted["blob"]
    assert redacted["note"] == "hello"


def test_redact_mapping_stops_at_depth_limit() -> None:
    deep: Any = {"leaf": "deep-secret-value"}
    for _ in range(8):
        deep = {"level": deep}

    rendered = json.dumps(redact_mapping(deep))

    assert "deep-secret-value" not in rendered
    assert "***" in rendered
    assert redact_mapping({"nested": {"a": 1}}, depth=6) == {"nested": "***"}
    assert redact_mapping({"nested": {"a": 1}}, depth=5) == {"nested": {"a": 1}}


def test_bearer_token_detection_under_any_key() -> None:
    samples = [
        "Bearer y0_AgAAAA",
        "eyJhbGciOiJIUzI1NiJ9",
        "ghp_abcdefghijkl",
        "sk-abcdefghijkl",
    ]

    redacted = redact_mapping({f"field_{index}": value for index, value in enumerate(samples)})
    rendered = json.dumps(redacted)

    for value in samples:
        assert value not in rendered
    assert redacted["field_0"].startswith("Bearer ")
    assert redacted["field_0"] != "Bearer y0_AgAAAA"


def test_redaction_filter_masks_format_arguments() -> None:
    record = make_record(msg="auth header %s", args=("Bearer y0_AgAAAA",))

    assert RedactionFilter().filter(record) is True
    assert record.getMessage() == "auth header Bearer ***"


def test_redaction_filter_masks_extra_fields() -> None:
    record = make_record(token="super-secret-value", safe="visible")

    RedactionFilter().filter(record)

    assert record.token == "su***ue"
    assert record.safe == "visible"


def test_json_formatter_shape_and_single_line() -> None:
    record = make_record(msg="hello %s", args=("world",), request_id="abc")

    line = JsonFormatter().format(record)

    assert "\n" not in line
    payload = json.loads(line)
    assert set(payload) == {"ts", "level", "logger", "msg", "extra"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "lv24_seo.test"
    assert payload["msg"] == "hello world"
    assert payload["extra"] == {"request_id": "abc"}
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_masks_message_and_extras() -> None:
    record = make_record(
        msg="login token=%s",
        args=("abc123",),
        token="abcdefghijklmno",
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["msg"] == "login token=***"
    assert payload["extra"]["token"] == "ab***no"
    assert "abcdefghijklmno" not in json.dumps(payload)


def test_json_formatter_masks_authorization_header() -> None:
    record = make_record("Authorization: Bearer y0_AgAAAA signed in")

    payload = json.loads(JsonFormatter().format(record))

    assert "y0_AgAAAA" not in payload["msg"]
    assert payload["msg"].startswith("Authorization: ***")


def test_json_formatter_serializes_unserializable_extra() -> None:
    record = make_record(payload=object(), tags={"a", "b"})

    payload = json.loads(JsonFormatter().format(record))

    assert "object at 0x" in payload["extra"]["payload"]
    assert sorted(payload["extra"]["tags"]) == ["a", "b"]


def test_json_formatter_survives_broken_str() -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    payload = json.loads(JsonFormatter().format(make_record(broken=Broken())))

    assert payload["extra"] == {}
    assert payload["msg"] == "message"


def test_setup_logging_is_idempotent() -> None:
    stream = BUF()
    for _ in range(3):
        setup_logging("DEBUG", json_output=True, stream=stream)

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    assert root.propagate is False

    get_logger("lv24_seo.test").debug("once")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["msg"] == "once"


def test_setup_logging_replaces_previous_stream() -> None:
    first, second = BUF(), BUF()

    setup_logging(stream=first)
    setup_logging(stream=second)
    get_logger("lv24_seo.test").info("routed")

    assert first.getvalue() == ""
    assert json.loads(second.getvalue())["msg"] == "routed"


def test_setup_logging_survives_closed_stream() -> None:
    stream = BUF()
    stream.close()

    setup_logging(stream=stream)
    get_logger("lv24_seo.test").info("after close")
    setup_logging(stream=stream)


def test_setup_logging_end_to_end_masks_authorization() -> None:
    stream = BUF()
    setup_logging(json_output=True, stream=stream)

    get_logger("lv24_seo.http").info("auth header %s", "Bearer y0_AgAAAA")

    raw = stream.getvalue()
    assert "y0_AgAAAA" not in raw
    assert json.loads(raw)["msg"] == "auth header Bearer ***"


def test_setup_logging_masks_extra_token_field() -> None:
    stream = BUF()
    setup_logging(json_output=True, stream=stream)

    get_logger("lv24_seo.test").info("login", extra={"token": "supersecretvalue", "user": "ann"})

    payload = json.loads(stream.getvalue())
    assert payload["extra"] == {"token": "su***ue", "user": "ann"}
    assert "supersecretvalue" not in stream.getvalue()


def test_setup_logging_text_output_keeps_message_and_masks_extra() -> None:
    stream = BUF()
    setup_logging("INFO", json_output=False, stream=stream)

    get_logger("lv24_seo.test").info("plain %s", "value", extra={"token": "supersecretvalue"})

    output = stream.getvalue()
    assert "plain value" in output
    assert "INFO" in output
    assert "supersecretvalue" not in output


def test_setup_logging_defaults_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging()

    get_logger("lv24_seo.test").info("to stderr")

    assert "to stderr" in capsys.readouterr().err
