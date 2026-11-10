"""Contract tests for secret storage: precedence, 0600 file, and no value leaks."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lv24_seo.core import secrets as secrets_module
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, WRITE_DISABLED, SeoError
from lv24_seo.core.secrets import SECRET_NAMES, SecretStatus, SecretStore

SERVICE = "lv24-seo"


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without a keyring; keyring tests opt in explicitly."""
    monkeypatch.setattr(secrets_module, "_keyring_backend", lambda: None)


class FakeKeyring:
    """In-memory stand-in for a keyring backend."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _write_secrets(data_dir: Path, payload: dict[str, object]) -> Path:
    path = data_dir / "secrets.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_secrets(data_dir: Path) -> object:
    return json.loads((data_dir / "secrets.json").read_text(encoding="utf-8"))


def test_secret_names_contract() -> None:
    assert SECRET_NAMES == (
        "gsc_token",
        "yandex_webmaster_token",
        "yandex_metrika_token",
        "bing_api_key",
        "crux_api_key",
        "psi_api_key",
        "smtp_password",
        "telegram_bot_token",
    )


def test_env_wins_over_file(tmp_path: Path) -> None:
    _write_secrets(tmp_path, {"bing_api_key": {"value": "file-value", "expires_at": None}})
    store = SecretStore(
        env={"LV24_SEO_BING_API_KEY": "env-value"},
        data_dir=tmp_path,
        allow_file=True,
    )

    assert store.get("bing_api_key") == "env-value"
    status = store.status(["bing_api_key"])[0]
    assert status == SecretStatus(name="bing_api_key", present=True, source="env")


def test_blank_env_value_is_treated_as_unset(tmp_path: Path) -> None:
    _write_secrets(tmp_path, {"gsc_token": {"value": "file-value", "expires_at": None}})
    store = SecretStore(
        env={"LV24_SEO_GSC_TOKEN": "   "},
        data_dir=tmp_path,
        allow_file=True,
    )

    assert store.get("gsc_token") == "file-value"


def test_file_round_trip(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"
    store = SecretStore(env={}, data_dir=data_dir, allow_file=True)

    assert store.get("crux_api_key") is None
    store.set("crux_api_key", "stored-value")
    assert store.get("crux_api_key") == "stored-value"
    assert _read_secrets(data_dir) == {
        "crux_api_key": {"value": "stored-value", "expires_at": None}
    }
    assert store.status(["crux_api_key"])[0] == SecretStatus(
        name="crux_api_key", present=True, source="file"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_file_mode_is_0600(tmp_path: Path) -> None:
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    store.set("psi_api_key", "stored-value")

    mode = stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode)
    assert mode == 0o600


def test_allow_file_defaults_to_env_flag(tmp_path: Path) -> None:
    store = SecretStore(env={"LV24_SEO_ALLOW_FILE_SECRETS": "yes"}, data_dir=tmp_path)

    store.set("bing_api_key", "stored-value")

    assert store.get("bing_api_key") == "stored-value"
    assert (tmp_path / "secrets.json").is_file()


def test_set_without_backends_is_write_disabled(tmp_path: Path) -> None:
    secret = "super-secret-token"
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=False)

    with pytest.raises(SeoError) as excinfo:
        store.set("bing_api_key", secret)

    assert excinfo.value.code == WRITE_DISABLED
    assert secret not in str(excinfo.value)
    assert secret not in excinfo.value.message
    assert secret not in (excinfo.value.next_action or "")
    assert not (tmp_path / "secrets.json").exists()


def test_status_reports_present_missing_and_source(tmp_path: Path) -> None:
    _write_secrets(
        tmp_path,
        {
            "yandex_metrika_token": {
                "value": "stored-value",
                "expires_at": "2027-01-02T03:04:05+00:00",
            }
        },
    )
    store = SecretStore(
        env={"LV24_SEO_BING_API_KEY": "env-value"},
        data_dir=tmp_path,
        allow_file=True,
    )

    statuses = store.status(["bing_api_key", "yandex_metrika_token", "gsc_token"])

    assert isinstance(statuses[0], SecretStatus)
    assert [(item.name, item.present, item.source) for item in statuses] == [
        ("bing_api_key", True, "env"),
        ("yandex_metrika_token", True, "file"),
        ("gsc_token", False, "missing"),
    ]
    assert statuses[0].expires_at is None
    assert statuses[1].expires_at == datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert statuses[2].expires_at is None


def test_status_defaults_to_all_known_names(tmp_path: Path) -> None:
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=False)

    statuses = store.status()

    assert [item.name for item in statuses] == list(SECRET_NAMES)
    assert {item.source for item in statuses} == {"missing"}


def test_repr_never_contains_values(tmp_path: Path) -> None:
    store = SecretStore(
        env={"LV24_SEO_BING_API_KEY": "env-hidden-value"},
        data_dir=tmp_path,
        allow_file=True,
    )
    store.set("crux_api_key", "file-hidden-value")

    text = repr(store)

    assert text.startswith("SecretStore(")
    assert "env-hidden-value" not in text
    assert "file-hidden-value" not in text


@pytest.mark.parametrize("name", ["", "gsc", "GSC_TOKEN", "unknown"])
def test_unknown_name_raises_invalid_input(tmp_path: Path, name: str) -> None:
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    with pytest.raises(SeoError) as excinfo:
        store.get(name)
    assert excinfo.value.code == INVALID_INPUT
    with pytest.raises(SeoError):
        store.set(name, "some-value")
    with pytest.raises(SeoError):
        store.delete(name)
    with pytest.raises(SeoError):
        store.status([name])


def test_legacy_flat_string_file_is_tolerated(tmp_path: Path) -> None:
    _write_secrets(tmp_path, {"bing_api_key": "legacy-value"})
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    assert store.get("bing_api_key") == "legacy-value"
    assert store.status(["bing_api_key"])[0].expires_at is None


def test_file_is_ignored_when_disabled(tmp_path: Path) -> None:
    _write_secrets(tmp_path, {"gsc_token": {"value": "file-value", "expires_at": None}})
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=False)

    assert store.get("gsc_token") is None
    assert store.status(["gsc_token"])[0].source == "missing"


def test_keyring_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeKeyring()
    backend.set_password(SERVICE, "gsc_token", "keyring-value")
    monkeypatch.setattr(secrets_module, "_keyring_backend", lambda: backend)
    _write_secrets(tmp_path, {"gsc_token": {"value": "file-value", "expires_at": None}})
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    assert store.get("gsc_token") == "keyring-value"
    assert store.status(["gsc_token"])[0].source == "keyring"


def test_set_prefers_keyring_and_delete_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeKeyring()
    monkeypatch.setattr(secrets_module, "_keyring_backend", lambda: backend)
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    store.set("smtp_password", "keyring-value")

    assert backend.values[(SERVICE, "smtp_password")] == "keyring-value"
    assert not (tmp_path / "secrets.json").exists()

    store.delete("smtp_password")

    assert (SERVICE, "smtp_password") not in backend.values


def test_env_secret_is_read_only(tmp_path: Path) -> None:
    store = SecretStore(
        env={"LV24_SEO_PSI_API_KEY": "env-value"},
        data_dir=tmp_path,
        allow_file=True,
    )

    with pytest.raises(SeoError) as write_error:
        store.set("psi_api_key", "other-value")
    assert write_error.value.code == WRITE_DISABLED
    with pytest.raises(SeoError) as delete_error:
        store.delete("psi_api_key")
    assert delete_error.value.code == WRITE_DISABLED
    assert not (tmp_path / "secrets.json").exists()


def test_delete_removes_only_requested_file_entry(tmp_path: Path) -> None:
    _write_secrets(
        tmp_path,
        {
            "crux_api_key": {"value": "stored-value", "expires_at": None},
            "future_key": "keep-me",
        },
    )
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    store.delete("crux_api_key")

    assert _read_secrets(tmp_path) == {"future_key": "keep-me"}


def test_corrupt_file_is_config_invalid(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("{not json", encoding="utf-8")
    store = SecretStore(env={}, data_dir=tmp_path, allow_file=True)

    with pytest.raises(SeoError) as excinfo:
        store.get("gsc_token")

    assert excinfo.value.code == CONFIG_INVALID
    assert "{not json" not in excinfo.value.message
