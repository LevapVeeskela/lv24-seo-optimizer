"""Offline CLI tests for the Yandex read commands (no provider is contacted)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.core.envelope import ok

RUNNER = CliRunner()
HOST_ID = "https:example.com:443"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        "\n"
        "[providers.yandex_webmaster]\n"
        f'host_id = "{HOST_ID}"\n',
        encoding="utf-8",
    )
    return path


class FakeService:
    """Record CLI arguments and return a minimal success envelope."""

    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, config: Any, store: Any = None) -> None:
        self.config = config
        self.store = store

    def _reply(self, name: str, rows: list[dict[str, Any]], **meta: Any) -> dict[str, Any]:
        self.calls.append((name, dict(meta)))
        return ok({"host_id": HOST_ID, "rows": rows, **meta}, meta={"total": len(rows)})

    def yandex_indexing(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "yandex_indexing",
                {"date_from": date_from, "date_to": date_to},
            )
        )
        return ok(
            {"host_id": HOST_ID, "summary": [], "history": [], "problems": [], "events": []},
            meta={"host_id": HOST_ID},
        )

    def yandex_samples(self, *, kind: str, limit: int, offset: int) -> dict[str, Any]:
        return self._reply(
            "yandex_samples",
            [{"status": "OK", "http_code": 200, "url": "https://example.com/a"}],
            kind=kind,
            limit=limit,
            offset=offset,
        )

    def yandex_sitemaps(
        self,
        *,
        sitemap_id: str,
        parent_id: str,
        limit: int,
        from_: str,
    ) -> dict[str, Any]:
        return self._reply(
            "yandex_sitemaps",
            [{"sitemap_id": "abc", "urls_count": 6532}],
            sitemap_id=sitemap_id,
            parent_id=parent_id,
            limit=limit,
            from_=from_,
        )

    def yandex_history(
        self,
        *,
        kind: str,
        query_id: str,
        date_from: date | None,
        date_to: date | None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "yandex_history",
                {
                    "kind": kind,
                    "query_id": query_id,
                    "date_from": date_from,
                    "date_to": date_to,
                },
            )
        )
        return ok(
            {
                "host_id": HOST_ID,
                "kind": kind,
                "rows": [{"date": "2026-11-01", "value": 42}],
            },
            meta={"total": 1},
        )

    def yandex_queue(
        self,
        *,
        task_id: str,
        limit: int,
        offset: int,
        date_from: date | None,
        date_to: date | None,
    ) -> dict[str, Any]:
        return self._reply(
            "yandex_queue",
            [{"task_id": "task-1", "state": "DONE", "url": "https://example.com/a"}],
            task_id=task_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        )


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeService]:
    FakeService.calls = []
    monkeypatch.setattr(cli_main, "SeoService", FakeService)
    return FakeService


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(getattr(result, "stdout", None) or result.output)


def test_yandex_samples_forwards_kind_limit_and_offset(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "samples",
        "--kind",
        "events",
        "--limit",
        "5",
        "--offset",
        "2",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["data"]["kind"] == "events"
    assert payload["data"]["rows"][0]["url"] == "https://example.com/a"
    assert fake_service.calls == [("yandex_samples", {"kind": "events", "limit": 5, "offset": 2})]


def test_yandex_sitemaps_reads_one_id(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "sitemaps",
        "--id",
        "abc",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["data"]["rows"][0]["sitemap_id"] == "abc"
    assert fake_service.calls == [
        (
            "yandex_sitemaps",
            {"sitemap_id": "abc", "parent_id": "", "limit": 100, "from_": ""},
        )
    ]


def test_yandex_history_parses_dates_and_query_id(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "history",
        "--kind",
        "query",
        "--query-id",
        "a08b",
        "--date-from",
        "2026-11-01",
        "--date-to",
        "2026-11-02",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["data"]["kind"] == "query"
    assert fake_service.calls == [
        (
            "yandex_history",
            {
                "kind": "query",
                "query_id": "a08b",
                "date_from": date(2026, 11, 1),
                "date_to": date(2026, 11, 2),
            },
        )
    ]


def test_yandex_queue_forwards_paging(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "queue",
        "--limit",
        "25",
        "--offset",
        "5",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["data"]["rows"][0]["task_id"] == "task-1"
    assert fake_service.calls == [
        (
            "yandex_queue",
            {
                "task_id": "",
                "limit": 25,
                "offset": 5,
                "date_from": None,
                "date_to": None,
            },
        )
    ]


def test_yandex_indexing_forwards_the_date_window(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "indexing",
        "--date-from",
        "2026-10-01",
        "--date-to",
        "2026-10-02",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    assert fake_service.calls == [
        (
            "yandex_indexing",
            {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 2)},
        )
    ]


def test_yandex_samples_human_output_is_stable(
    fake_service: type[FakeService],
    workspace: Path,
) -> None:
    result = _invoke(
        "yandex",
        "samples",
        "--kind",
        "indexing",
        "--config",
        str(workspace),
    )

    assert result.exit_code == 0
    output = getattr(result, "stdout", None) or result.output
    assert "yandex samples (indexing): 1 rows" in output
    assert "https://example.com/a" in output
