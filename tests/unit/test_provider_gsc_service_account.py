"""Offline tests for GSC service-account authentication.

The RSA key is generated in-test, the token endpoint is an ``httpx.MockTransport`` and
no request leaves the process. The tests pin the JWT header/claims, the token cache and
refresh margin, the resolution order used by the service, and the security contract:
the private key never appears in an error, ``repr`` or JSON envelope.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from typer.testing import CliRunner

from lv24_seo import services
from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import (
    GscConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
    load_config,
)
from lv24_seo.core.errors import AUTH_INVALID, CONFIG_INVALID, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.gsc.auth import (
    JWT_BEARER_GRANT_TYPE,
    WEBMASTERS_READONLY_SCOPE,
    ServiceAccountCredentials,
    ServiceAccountTokenProvider,
    StaticTokenProvider,
    load_service_account_file,
    service_account_file_problem,
    token_provider_from_token,
)
from lv24_seo.services import SeoService

Handler = Callable[[httpx.Request], httpx.Response]
PROPERTY = "sc-domain:example.com"
EMAIL = "lv24-gsc-reader@lv24-seo-tools.iam.gserviceaccount.com"
KEY_ID = "key-id-1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
RUNNER = CliRunner()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _document(key: rsa.RSAPrivateKey, **overrides: Any) -> dict[str, Any]:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    document: dict[str, Any] = {
        "type": "service_account",
        "project_id": "lv24-seo-tools",
        "private_key_id": KEY_ID,
        "private_key": pem,
        "client_email": EMAIL,
        "client_id": "1234567890",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URI,
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/reader",
    }
    document.update(overrides)
    return document


def _write_key(tmp_path: Path, key: rsa.RSAPrivateKey, **overrides: Any) -> Path:
    path = tmp_path / "gsc-service-account.json"
    path.write_text(json.dumps(_document(key, **overrides)), encoding="utf-8")
    return path


def _decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _token_transport(
    calls: list[httpx.Request],
    *,
    token: str = "ya29.access-token",
    expires_in: int = 3600,
) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        )

    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)


def _sites_transport(seen: list[httpx.Request]) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"siteEntry": [{"siteUrl": PROPERTY, "permissionLevel": "siteOwner"}]},
        )

    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)


def _config(tmp_path: Path, *, service_account_file: str = "") -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example",
            base_url="https://example.com",
            default_property=PROPERTY,
        ),
        providers=ProvidersConfig(
            gsc=GscConfig(property=PROPERTY, service_account_file=service_account_file)
        ),
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


def _secrets(tmp_path: Path, **values: str) -> SecretStore:
    env = {f"LV24_SEO_{name.upper()}": value for name, value in values.items()}
    return SecretStore(env=env, data_dir=tmp_path / "data", allow_file=False)


# -- JWT and token exchange -------------------------------------------------------------------


def test_service_account_signs_rs256_jwt_and_exchanges_it(tmp_path: Path, rsa_key: Any) -> None:
    path = _write_key(tmp_path, rsa_key)
    token_calls: list[httpx.Request] = []
    sites_seen: list[httpx.Request] = []
    provider = ServiceAccountTokenProvider(
        load_service_account_file(str(path)),
        http=_token_transport(token_calls),
    )
    client = GscClient(
        token_provider=provider,
        http=_sites_transport(sites_seen),
        property_id=PROPERTY,
    )

    result = client.list_sites()

    assert result.rows == [{"site_url": PROPERTY, "permission_level": "siteOwner"}]
    assert len(token_calls) == 1
    assert str(token_calls[0].url) == TOKEN_URI
    form = parse_qs(token_calls[0].content.decode("utf-8"))
    assert form["grant_type"] == [JWT_BEARER_GRANT_TYPE]
    assertion = form["assertion"][0]
    header_part, claims_part, signature_part = assertion.split(".")
    header = json.loads(_decode(header_part))
    claims = json.loads(_decode(claims_part))

    assert header == {"alg": "RS256", "kid": KEY_ID, "typ": "JWT"}
    assert claims["iss"] == EMAIL
    assert claims["scope"] == WEBMASTERS_READONLY_SCOPE
    assert claims["aud"] == TOKEN_URI
    assert isinstance(claims["iat"], int)
    assert claims["exp"] == claims["iat"] + 3600
    rsa_key.public_key().verify(
        _decode(signature_part),
        f"{header_part}.{claims_part}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert sites_seen[0].headers["Authorization"] == "Bearer ya29.access-token"


def test_token_is_cached_until_the_refresh_margin(tmp_path: Path, rsa_key: Any) -> None:
    path = _write_key(tmp_path, rsa_key)
    now = [1000.0]
    calls: list[httpx.Request] = []
    provider = ServiceAccountTokenProvider(
        load_service_account_file(str(path)),
        http=_token_transport(calls, token="token-1", expires_in=3600),
        clock=lambda: now[0],
    )

    assert provider.access_token() == "token-1"
    assert provider.access_token() == "token-1"
    assert len(calls) == 1

    # expires_at = 4600; refresh_at = 4540 (60 seconds before expiry).
    now[0] = 4539.0
    assert provider.access_token() == "token-1"
    assert len(calls) == 1

    now[0] = 4540.0
    assert provider.access_token() == "token-1"
    assert len(calls) == 2


def test_long_lived_client_refreshes_the_token_per_request(tmp_path: Path, rsa_key: Any) -> None:
    path = _write_key(tmp_path, rsa_key)
    now = [1000.0]
    tokens: list[str] = []

    def token_handler(_request: httpx.Request) -> httpx.Response:
        tokens.append(f"token-{len(tokens) + 1}")
        return httpx.Response(200, json={"access_token": tokens[-1], "expires_in": 3600})

    provider = ServiceAccountTokenProvider(
        load_service_account_file(str(path)),
        http=HttpClient(transport=httpx.MockTransport(token_handler), sleep=lambda _seconds: None),
        clock=lambda: now[0],
    )
    seen: list[httpx.Request] = []
    client = GscClient(token_provider=provider, http=_sites_transport(seen), property_id=PROPERTY)

    client.list_sites()
    now[0] = 4600.0
    client.list_sites()

    assert [request.headers["Authorization"] for request in seen] == [
        "Bearer token-1",
        "Bearer token-2",
    ]


def test_token_endpoint_rejection_is_auth_invalid_without_echoing_the_response(
    tmp_path: Path, rsa_key: Any
) -> None:
    path = _write_key(tmp_path, rsa_key)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})

    provider = ServiceAccountTokenProvider(
        load_service_account_file(str(path)),
        http=HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None),
    )

    with pytest.raises(SeoError) as excinfo:
        provider.access_token()

    assert excinfo.value.code == AUTH_INVALID
    assert "invalid_grant" not in excinfo.value.message
    assert "revoked" not in excinfo.value.message


# -- file validation --------------------------------------------------------------------------


def test_missing_file_is_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(SeoError) as excinfo:
        load_service_account_file(str(path))

    assert excinfo.value.code == CONFIG_INVALID
    assert "not found" in excinfo.value.message
    assert str(path) in excinfo.value.message
    assert excinfo.value.next_action


def test_relative_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gsc.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SeoError) as excinfo:
        load_service_account_file("gsc.json")

    assert excinfo.value.code == CONFIG_INVALID
    assert "absolute" in excinfo.value.message
    assert service_account_file_problem("gsc.json") == excinfo.value.message


def test_invalid_json_is_config_invalid_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "gsc.json"
    path.write_text('{"type": "service_account", "private_key": "SECRET', encoding="utf-8")

    with pytest.raises(SeoError) as excinfo:
        load_service_account_file(str(path))

    assert excinfo.value.code == CONFIG_INVALID
    assert "not valid JSON" in excinfo.value.message
    assert "SECRET" not in excinfo.value.message


def test_unreadable_file_is_config_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_key(tmp_path, rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def fail_read(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(SeoError) as excinfo:
        load_service_account_file(str(path))

    assert excinfo.value.code == CONFIG_INVALID
    assert "cannot read" in excinfo.value.message


def test_invalid_private_key_is_config_invalid(tmp_path: Path, rsa_key: Any) -> None:
    path = _write_key(
        tmp_path,
        rsa_key,
        private_key="-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
    )
    provider = ServiceAccountTokenProvider(load_service_account_file(str(path)))

    with pytest.raises(SeoError) as excinfo:
        provider.access_token()

    assert excinfo.value.code == CONFIG_INVALID
    assert "not-a-key" not in excinfo.value.message


# -- gsc_token forms --------------------------------------------------------------------------


def test_plain_gsc_token_keeps_the_bearer_behaviour() -> None:
    provider = token_provider_from_token("plain-oauth-token")

    assert isinstance(provider, StaticTokenProvider)
    assert provider.access_token() == "plain-oauth-token"

    seen: list[httpx.Request] = []
    client = GscClient(
        access_token="plain-oauth-token", http=_sites_transport(seen), property_id=PROPERTY
    )
    client.list_sites()
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer plain-oauth-token"


def test_gsc_token_may_be_a_service_account_json_object(rsa_key: Any) -> None:
    provider = token_provider_from_token(json.dumps(_document(rsa_key)))

    assert isinstance(provider, ServiceAccountTokenProvider)


def test_json_that_is_not_a_service_account_key_is_rejected() -> None:
    with pytest.raises(SeoError) as excinfo:
        token_provider_from_token(json.dumps({"type": "authorized_user", "refresh_token": "x"}))

    assert excinfo.value.code == CONFIG_INVALID
    assert "service-account" in excinfo.value.message


def test_credentials_and_provider_repr_never_include_the_key(rsa_key: Any) -> None:
    document = _document(rsa_key)
    credentials = ServiceAccountCredentials(
        client_email=EMAIL,
        private_key=document["private_key"],
        private_key_id=KEY_ID,
    )
    provider = ServiceAccountTokenProvider(credentials)

    assert "PRIVATE KEY" not in repr(credentials)
    assert "PRIVATE KEY" not in repr(provider)
    assert EMAIL in repr(credentials)


# -- service resolution and readiness ---------------------------------------------------------


def test_env_service_account_file_creates_the_gsc_block(tmp_path: Path) -> None:
    path = tmp_path / "gsc-service-account.json"

    config = load_config(cwd=tmp_path, env={"LV24_SEO_GSC_SERVICE_ACCOUNT_FILE": str(path)})

    assert config.providers.gsc is not None
    assert config.providers.gsc.service_account_file == str(path)


def test_properties_marks_gsc_ready_from_the_service_account_file(
    tmp_path: Path, rsa_key: Any
) -> None:
    path = _write_key(tmp_path, rsa_key)
    service = SeoService(
        _config(tmp_path, service_account_file=str(path)), secrets=_secrets(tmp_path)
    )

    response = service.properties()

    assert response["ok"] is True
    entries = {entry["provider"]: entry for entry in response["data"]["providers"]}
    assert entries["gsc"]["token"] == "present"
    assert entries["gsc"]["token_present"] is True
    assert entries["gsc"]["token_source"] == "service_account_file"
    assert entries["gsc"]["ready"] is True
    assert "gsc" in response["meta"]["ready"]
    assert "PRIVATE KEY" not in json.dumps(response)
    assert "private_key" not in json.dumps(response)


def test_properties_reports_a_relative_file_as_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    service = SeoService(
        _config(tmp_path, service_account_file="gsc.json"), secrets=_secrets(tmp_path)
    )

    response = service.properties()

    entries = {entry["provider"]: entry for entry in response["data"]["providers"]}
    assert entries["gsc"]["token_present"] is False
    assert entries["gsc"]["ready"] is False
    assert any("absolute" in warning for warning in response["warnings"])


def test_service_prefers_the_service_account_file_over_a_stored_token(
    tmp_path: Path, rsa_key: Any
) -> None:
    path = _write_key(tmp_path, rsa_key)
    service = SeoService(
        _config(tmp_path, service_account_file=str(path)),
        secrets=_secrets(tmp_path, gsc_token="stale-access-token"),
    )

    client = service._wired_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, ServiceAccountTokenProvider)


def test_service_reads_the_key_file_once_per_process(
    tmp_path: Path, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_key(tmp_path, rsa_key)
    calls: list[str] = []
    original = services.load_service_account_file

    def counted(raw: str) -> ServiceAccountCredentials:
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(services, "load_service_account_file", counted)
    service = SeoService(
        _config(tmp_path, service_account_file=str(path)), secrets=_secrets(tmp_path)
    )

    first = service._wired_client("gsc")
    second = service._wired_client("gsc")

    assert first is second
    assert calls == [str(path)]


def test_service_builds_a_provider_from_a_json_gsc_token(tmp_path: Path, rsa_key: Any) -> None:
    service = SeoService(
        _config(tmp_path),
        secrets=_secrets(tmp_path, gsc_token=json.dumps(_document(rsa_key))),
    )

    client = service._wired_client("gsc")

    assert isinstance(client, GscClient)
    assert isinstance(client._tokens, ServiceAccountTokenProvider)


def test_gsc_query_reports_a_missing_service_account_file_as_config_invalid(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    service = SeoService(
        _config(tmp_path, service_account_file=str(missing)), secrets=_secrets(tmp_path)
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 2))

    assert response["ok"] is False
    assert response["error"]["code"] == CONFIG_INVALID
    assert "not found" in response["error"]["message"]
    assert response["error"]["next_action"]


def test_doctor_reports_gsc_auth_from_the_service_account_file(
    tmp_path: Path, rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_key(tmp_path, rsa_key)
    (tmp_path / "data").mkdir()
    config_path = tmp_path / "seo.toml"
    config_path.write_text(
        "[providers.gsc]\n"
        f"property = {json.dumps(PROPERTY)}\n"
        f"service_account_file = {json.dumps(str(path))}\n",
        encoding="utf-8",
    )
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    result = RUNNER.invoke(cli_main.app, ["doctor", "--config", str(config_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    check = next(item for item in payload["data"]["checks"] if item["id"] == "gsc_auth")
    assert check["ok"] is True
    assert str(path) in check["detail"]
    tokens = next(item for item in payload["data"]["checks"] if item["id"] == "tokens")
    assert tokens["ok"] is True
    assert "PRIVATE KEY" not in result.output
