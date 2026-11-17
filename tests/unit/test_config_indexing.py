"""``[indexing]`` configuration: defaults, strict validation and deduplication."""

from __future__ import annotations

from pathlib import Path

import pytest

from lv24_seo.core.config import IndexingConfig, load_config
from lv24_seo.core.errors import CONFIG_INVALID, SeoError


def test_indexing_defaults_are_disabled_and_bounded() -> None:
    config = IndexingConfig()

    assert config.enabled is False
    assert config.providers == ["bing", "yandex", "gsc", "indexnow"]
    assert config.max_submit_per_run == 500
    assert config.max_inspect_per_run == 20
    assert config.stale_days == 7
    assert config.check_interval_days == 7
    assert config.gsc_inspect_daily_limit == 2_000
    assert config.indexnow_daily_limit == 1_000


def test_indexing_providers_are_deduplicated_in_order() -> None:
    config = IndexingConfig(providers=["gsc", "bing", "gsc"])

    assert config.providers == ["gsc", "bing"]


@pytest.mark.parametrize(
    "body",
    [
        '[indexing]\nproviders = ["google"]\n',
        "[indexing]\nmax_submit_per_run = -1\n",
        "[indexing]\nmax_inspect_per_run = 5000\n",
        "[indexing]\nunknown = true\n",
    ],
)
def test_indexing_rejects_invalid_values(tmp_path: Path, body: str) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(SeoError) as excinfo:
        load_config(path, env={})

    assert excinfo.value.code == CONFIG_INVALID


def test_indexing_block_loads_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'base_url = "https://example.com"\n'
        "\n"
        "[indexing]\n"
        "enabled = true\n"
        'providers = ["bing", "indexnow"]\n'
        "max_submit_per_run = 25\n"
        "max_inspect_per_run = 3\n"
        "stale_days = 14\n"
        "check_interval_days = 3\n"
        "gsc_inspect_daily_limit = 100\n"
        "indexnow_daily_limit = 50\n",
        encoding="utf-8",
    )

    config = load_config(path, env={})

    assert config.indexing.enabled is True
    assert config.indexing.providers == ["bing", "indexnow"]
    assert config.indexing.max_submit_per_run == 25
    assert config.indexing.max_inspect_per_run == 3
    assert config.indexing.stale_days == 14
    assert config.indexing.check_interval_days == 3
    assert config.indexing.gsc_inspect_daily_limit == 100
    assert config.indexing.indexnow_daily_limit == 50
