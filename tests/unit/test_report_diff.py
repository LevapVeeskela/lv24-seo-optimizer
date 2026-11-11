"""Week-over-week snapshot diff: honest metric deltas, issue/opportunity churn, Markdown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.core.config import ProjectConfig, ProvidersConfig, SeoConfig, StorageConfig
from lv24_seo.core.secrets import SecretStore
from lv24_seo.report.diff import diff_snapshots, render_diff_markdown
from lv24_seo.services import SeoService
from lv24_seo.store import Store

PROPERTY = "sc-domain:example.com"


def _snapshot(
    start: str,
    end: str,
    *,
    clicks: float | None = 100.0,
    impressions: float | None = 1000.0,
    issues: dict[str, int] | None = None,
    opportunities: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    limitations: list[str] = (),  # type: ignore[assignment]
) -> dict[str, Any]:
    headline = []
    for metric, value in (("clicks", clicks), ("impressions", impressions)):
        if value is not None:
            headline.append({"metric": metric, "current": value, "previous": value})
    return {
        "kind": "weekly",
        "property_id": PROPERTY,
        "period": {"start": start, "end": end},
        "headline": "h",
        "sections": [
            {"key": "headline", "rows": headline},
            {
                "key": "issues",
                "rows": [
                    {"severity": sev, "count": count, "rule_ids": []}
                    for sev, count in (issues or {}).items()
                ],
            },
            {
                "key": "opportunities",
                "rows": [{"kind": k, "query": q, "page": p} for k, q, p in opportunities],
            },
        ],
        "limitations": list(limitations),
        "warnings": [],
    }


def test_metric_deltas_and_churn() -> None:
    base = _snapshot(
        "2026-10-26",
        "2026-11-01",
        clicks=100.0,
        issues={"high": 3, "low": 1},
        opportunities=[("decay", "seo", "/a"), ("ctr_gap", "audit", "/b")],
        limitations=["old caveat"],
    )
    head = _snapshot(
        "2026-11-02",
        "2026-11-08",
        clicks=80.0,
        issues={"high": 1, "medium": 2, "low": 1},
        opportunities=[("decay", "seo", "/a"), ("striking_distance", "crawl", "/c")],
        limitations=["new caveat"],
    )

    diff = diff_snapshots(base, head)

    clicks = next(row for row in diff["headline"] if row["metric"] == "clicks")
    assert (clicks["abs_delta"], clicks["pct_delta"]) == (-20.0, -20.0)
    assert diff["comparable"] is True
    assert {row["severity"]: row["delta"] for row in diff["issues"]} == {
        "high": -2,
        "low": 0,
        "medium": 2,
    }
    assert diff["opportunities"]["added"] == [
        {"kind": "striking_distance", "query": "crawl", "page": "/c"}
    ]
    assert diff["opportunities"]["resolved"] == [
        {"kind": "ctr_gap", "query": "audit", "page": "/b"}
    ]
    assert diff["opportunities"]["unchanged"] == 1
    assert diff["limitations_changed"] == {"added": ["new caveat"], "lifted": ["old caveat"]}


def test_metric_missing_on_one_side_is_unknown_not_zero() -> None:
    base = _snapshot("2026-10-26", "2026-11-01", impressions=None)
    head = _snapshot("2026-11-02", "2026-11-08")
    diff = diff_snapshots(base, head)
    assert [row["metric"] for row in diff["headline"]] == ["clicks"]
    assert diff["unknown_metrics"] == ["impressions"]
    assert diff["comparable"] is False
    assert any("unknown, not zero" in note for note in diff["limitations"])


def test_no_headline_or_different_lengths_is_not_comparable() -> None:
    empty = _snapshot("2026-10-26", "2026-11-01", clicks=None, impressions=None)
    head = _snapshot("2026-11-02", "2026-11-08")
    assert diff_snapshots(empty, head)["comparable"] is False

    longer = _snapshot("2026-10-05", "2026-11-01")
    diff = diff_snapshots(longer, head)
    assert diff["comparable"] is False
    assert any("28d vs 7d" in note for note in diff["limitations"])


def test_markdown_is_deterministic_and_carries_caveats() -> None:
    base = _snapshot("2026-10-26", "2026-11-01", impressions=None, issues={"high": 2})
    head = _snapshot(
        "2026-11-02", "2026-11-08", issues={"high": 1}, opportunities=[("decay", "q", "/p")]
    )
    first = render_diff_markdown(diff_snapshots(base, head))
    second = render_diff_markdown(diff_snapshots(base, head))
    assert first == second
    assert "2026-10-26..2026-11-01 → 2026-11-02..2026-11-08" in first
    assert "| clicks | 100 | 100 | +0 | +0% |" in first
    assert "high 2→1 (-1)" in first
    assert "**New opportunities (1):**" in first
    assert "**Caveats:**" in first and "unknown, not zero" in first


def _config(data_dir: Path) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Ex", base_url="https://example.com/", default_property=PROPERTY
        ),
        providers=ProvidersConfig(),
        storage=StorageConfig(data_dir=data_dir),
    )


def test_service_diffs_the_two_newest_snapshots(tmp_path: Path) -> None:
    with Store(tmp_path / "seo.db") as store:
        store.save_snapshot(
            "weekly",
            PROPERTY,
            "2026-10-19..2026-10-25",
            _snapshot("2026-10-19", "2026-10-25", clicks=1.0),
        )
        store.save_snapshot(
            "weekly",
            PROPERTY,
            "2026-10-26..2026-11-01",
            _snapshot("2026-10-26", "2026-11-01", clicks=50.0),
        )
        store.save_snapshot(
            "weekly",
            PROPERTY,
            "2026-11-02..2026-11-08",
            _snapshot("2026-11-02", "2026-11-08", clicks=75.0),
        )
        service = SeoService(_config(tmp_path), store=store, secrets=SecretStore(env={}))

        response = service.report_diff()
        explicit = service.report_diff(base="2026-10-19..2026-10-25")

    assert response["ok"] is True
    assert response["meta"]["base_period"] == "2026-10-26..2026-11-01"
    assert response["meta"]["head_period"] == "2026-11-02..2026-11-08"
    assert response["meta"]["complete"] is True
    clicks = next(row for row in response["data"]["headline"] if row["metric"] == "clicks")
    assert clicks["pct_delta"] == 50.0
    assert "### SEO report diff" in response["data"]["markdown"]
    assert explicit["meta"]["base_period"] == "2026-10-19..2026-10-25"


def test_service_needs_two_snapshots_and_a_store(tmp_path: Path) -> None:
    with Store(tmp_path / "seo.db") as store:
        store.save_snapshot(
            "weekly", PROPERTY, "2026-11-02..2026-11-08", _snapshot("2026-11-02", "2026-11-08")
        )
        response = SeoService(
            _config(tmp_path), store=store, secrets=SecretStore(env={})
        ).report_diff()
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_INPUT"

    storeless = SeoService(_config(tmp_path), secrets=SecretStore(env={})).report_diff()
    assert storeless["ok"] is False
    assert storeless["error"]["code"] == "CONFIG_INVALID"


def test_cli_markdown_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lv24_seo.cli.main import app
    from lv24_seo.composition import DB_FILENAME

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_file = tmp_path / "seo.toml"
    config_file.write_text(
        "[project]\n"
        'name = "Ex"\n'
        'base_url = "https://example.com/"\n'
        f'default_property = "{PROPERTY}"\n'
        "[storage]\n"
        f"data_dir = {json.dumps(str(data_dir))}\n",
        encoding="utf-8",
    )
    with Store(data_dir / DB_FILENAME) as store:
        store.save_snapshot(
            "weekly", PROPERTY, "2026-10-26..2026-11-01", _snapshot("2026-10-26", "2026-11-01")
        )
        store.save_snapshot(
            "weekly",
            PROPERTY,
            "2026-11-02..2026-11-08",
            _snapshot("2026-11-02", "2026-11-08", clicks=120.0),
        )

    runner = CliRunner()
    md = runner.invoke(app, ["report", "diff", "--markdown", "--config", str(config_file)])
    assert md.exit_code == 0, md.stdout
    assert md.stdout.startswith("### SEO report diff")
    assert "| clicks | 100 | 120 | +20 | +20% |" in md.stdout

    js = runner.invoke(app, ["report", "diff", "--json", "--config", str(config_file)])
    envelope = json.loads(js.stdout)
    assert set(envelope) == {"ok", "data", "meta", "warnings"}
    assert envelope["meta"]["complete"] is True
