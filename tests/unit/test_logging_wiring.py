"""The redacting log handler is wired into process entry points, never into library imports.

``setup_logging`` replaces every root handler, so a library that calls it on import silently
reconfigures its embedder (the host's formatter, its log file, pytest's capture). These tests
pin the split: ``lv24-seo …`` and ``lv24-seo mcp serve`` install the handler when
``LV24_SEO_LOG_LEVEL`` is set, everything else leaves the root logger untouched.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lv24_seo.core.logging import (
    LOG_LEVEL_ENV,
    RedactionFilter,
    configure_logging_from_env,
    get_logger,
)

_SECRET = "y0_super-secret-token-value"


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


def _redacting_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if any(isinstance(item, RedactionFilter) for item in handler.filters)
    ]


def test_unset_variable_leaves_the_root_logger_alone() -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    assert configure_logging_from_env({}) is False
    assert configure_logging_from_env({LOG_LEVEL_ENV: "   "}) is False

    assert list(root.handlers) == before


def test_set_variable_installs_one_redacting_handler() -> None:
    stream = io.StringIO()

    assert configure_logging_from_env({LOG_LEVEL_ENV: "debug"}, stream=stream) is True

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert len(_redacting_handlers()) == 1
    assert logging.getLogger().level == logging.DEBUG

    get_logger("lv24_seo.test").warning("token=%s", _SECRET)
    line = stream.getvalue().strip()
    assert line, "the handler must write the record"
    assert _SECRET not in line
    assert json.loads(line)["level"] == "WARNING"


def test_repeated_calls_replace_instead_of_stacking() -> None:
    configure_logging_from_env({LOG_LEVEL_ENV: "INFO"}, stream=io.StringIO())
    configure_logging_from_env({LOG_LEVEL_ENV: "INFO"}, stream=io.StringIO())

    assert len(logging.getLogger().handlers) == 1


def test_cli_entry_point_installs_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from lv24_seo.cli.main import app

    before = list(logging.getLogger().handlers)
    result = CliRunner().invoke(app, ["version"], env={LOG_LEVEL_ENV: "WARNING"})

    assert result.exit_code == 0, result.output
    assert list(logging.getLogger().handlers) != before
    assert len(_redacting_handlers()) == 1


def test_cli_entry_point_is_a_no_op_without_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from lv24_seo.cli.main import app

    before = list(logging.getLogger().handlers)
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert list(logging.getLogger().handlers) == before


def test_mcp_transport_installs_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from lv24_seo.mcp import server as server_module

    class _StubServer:
        async def run_stdio_async(self) -> None:
            return None

    monkeypatch.setattr(server_module, "create_server", lambda: _StubServer())
    monkeypatch.setenv(LOG_LEVEL_ENV, "WARNING")

    asyncio.run(server_module.serve())

    assert len(_redacting_handlers()) == 1


def test_mcp_server_factory_is_library_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_server`` is what embedders call: it must not touch logging."""
    from lv24_seo.mcp import server as server_module

    class _StubService:
        timezone = "UTC"

    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
    before = list(logging.getLogger().handlers)

    server_module.create_server(_StubService())

    assert list(logging.getLogger().handlers) == before


def test_importing_the_package_never_reconfigures_logging() -> None:
    """A clean interpreter: importing every public module must leave handlers empty."""
    script = (
        "import logging, sys\n"
        "import lv24_seo, lv24_seo.cli.main, lv24_seo.mcp.server, lv24_seo.services\n"
        "print(len(logging.getLogger().handlers))\n"
    )
    env = {**os.environ, LOG_LEVEL_ENV: "DEBUG"}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"
