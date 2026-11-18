"""Unit tests for the ``tls-certificate`` rule (offline, frozen clock)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from lv24_seo.crawl.models import Issue, PageData, RuleContext
from lv24_seo.crawl.rules import load_rules, rule_tls_certificate

_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_UNSET: Any = object()


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rule_tls_certificate, "_utcnow", lambda: _NOW)


def _tls(**overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "version": "TLSv1.3",
        "cipher": "TLS_AES_128_GCM_SHA256",
        "not_after": "2027-11-18T12:00:00+00:00",
        "issuer": {"commonName": "R11"},
        "subject": {"commonName": "example.com"},
        "san": ["example.com", "www.example.com"],
    }
    facts.update(overrides)
    return facts


def _page(
    url: str = "https://example.com/",
    *,
    status: int = 200,
    content_type: str = "text/html",
    final_url: str | None = None,
    tls: Any = _UNSET,
) -> PageData:
    return PageData(
        url=url,
        status=status,
        final_url=url if final_url is None else final_url,
        content_type=content_type,
        tls=_tls() if tls is _UNSET else tls,
    )


def _run(*pages: PageData) -> list[Issue]:
    return rule_tls_certificate.RULE.run(
        RuleContext(pages=list(pages), site_url="https://example.com/")
    )


def _details(issues: list[Issue]) -> list[dict[str, Any]]:
    return [issue.detail for issue in issues]


def test_rule_contract_and_discovery() -> None:
    rule = rule_tls_certificate.RULE
    assert rule.id == "tls-certificate"
    assert rule.severity == "high"
    assert rule.title
    assert rule.description
    assert {candidate.id: candidate for candidate in load_rules()}["tls-certificate"] is rule


# --- expiry ----------------------------------------------------------------


def test_expired_certificate_is_reported() -> None:
    issues = _run(_page(tls=_tls(not_after="2026-11-01T12:00:00+00:00")))
    assert _details(issues) == [
        {"check": "expired", "not_after": "2026-11-01T12:00:00+00:00", "days_left": -17}
    ]


def test_certificate_expiring_in_ten_days_is_reported() -> None:
    issues = _run(_page(tls=_tls(not_after="2026-11-28T12:00:00+00:00")))
    assert _details(issues) == [
        {"check": "expiring", "not_after": "2026-11-28T12:00:00+00:00", "days_left": 10}
    ]


def test_certificate_expiring_in_twenty_days_is_expiring_soon() -> None:
    issues = _run(_page(tls=_tls(not_after="2026-12-08T12:00:00+00:00")))
    assert _details(issues) == [
        {"check": "expiring_soon", "not_after": "2026-12-08T12:00:00+00:00", "days_left": 20}
    ]


def test_certificate_valid_for_forty_days_passes() -> None:
    assert _run(_page(tls=_tls(not_after="2026-12-28T12:00:00+00:00"))) == []


@pytest.mark.parametrize("not_after", [None, "", "not-a-date", "2026-13-01T00:00:00+00:00", 12345])
def test_missing_or_malformed_expiry_is_unknown(not_after: object) -> None:
    issues = _run(_page(tls=_tls(not_after=not_after)))
    assert _details(issues) == [{"check": "unknown_expiry"}]


# --- protocol --------------------------------------------------------------


@pytest.mark.parametrize("version", ["TLSv1", "TLSv1.1"])
def test_legacy_tls_protocol_is_flagged(version: str) -> None:
    issues = _run(_page(tls=_tls(version=version)))
    assert _details(issues) == [{"check": "protocol", "version": version}]


@pytest.mark.parametrize("version", ["TLSv1.2", "TLSv1.3"])
def test_modern_tls_protocol_passes(version: str) -> None:
    assert _run(_page(tls=_tls(version=version))) == []


# --- hostname --------------------------------------------------------------


def test_wildcard_san_matches_single_label_subdomain() -> None:
    page = _page("https://a.example.com/", tls=_tls(san=["*.example.com"]))
    assert _run(page) == []


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://example.com/", "example.com"),
        ("https://a.b.example.com/", "a.b.example.com"),
    ],
)
def test_wildcard_san_rejects_apex_and_nested_hosts(url: str, host: str) -> None:
    issues = _run(_page(url, tls=_tls(san=["*.example.com"])))
    assert _details(issues) == [{"check": "san_mismatch", "host": host, "san": ["*.example.com"]}]


def test_san_mismatch_reports_host_and_san() -> None:
    issues = _run(_page(tls=_tls(san=["other.example", "www.other.example"])))
    assert _details(issues) == [
        {
            "check": "san_mismatch",
            "host": "example.com",
            "san": ["other.example", "www.other.example"],
        }
    ]


def test_common_name_is_used_when_san_is_empty() -> None:
    page = _page(tls=_tls(san=[], subject={"commonName": "example.com"}))
    assert _run(page) == []


def test_wildcard_common_name_is_supported() -> None:
    page = _page(
        "https://a.example.com/",
        tls=_tls(san=[], subject={"commonName": "*.example.com"}),
    )
    assert _run(page) == []


def test_common_name_mismatch_is_reported() -> None:
    issues = _run(_page(tls=_tls(san=[], subject={"commonName": "other.example"})))
    assert _details(issues) == [{"check": "san_mismatch", "host": "example.com", "san": []}]


def test_missing_san_and_common_name_is_unknown() -> None:
    issues = _run(_page(tls=_tls(san=[], subject={})))
    assert _details(issues) == [{"check": "unknown_san"}]


def test_san_list_is_truncated() -> None:
    san = [f"host{index}.example" for index in range(30)]
    detail = _run(_page(tls=_tls(san=san)))[0].detail
    assert detail["check"] == "san_mismatch"
    assert detail["san"] == san[:20]


def test_hostname_comes_from_the_final_url() -> None:
    page = _page("http://example.com/", final_url="https://www.example.com/")
    assert _run(page) == []


# --- cipher ----------------------------------------------------------------


@pytest.mark.parametrize(
    "cipher",
    [
        "TLS_RSA_WITH_RC4_128_SHA",
        "TLS_RSA_WITH_3DES_EDE_CBC_SHA",
        "TLS_RSA_WITH_NULL_SHA",
        "TLS_RSA_EXPORT_WITH_RC4_40_MD5",
    ],
)
def test_weak_cipher_is_flagged(cipher: str) -> None:
    issues = _run(_page(tls=_tls(cipher=cipher)))
    assert _details(issues) == [{"check": "weak_cipher", "cipher": cipher}]


def test_strong_cipher_passes() -> None:
    assert _run(_page(tls=_tls(cipher="TLS_AES_256_GCM_SHA384"))) == []


# --- page eligibility ------------------------------------------------------


def test_missing_tls_on_https_page_is_reported_as_unknown() -> None:
    issues = _run(_page(tls=None))
    assert _details(issues) == [{"check": "unknown"}]
    assert issues[0].url == "https://example.com/"
    assert issues[0].rule_id == "tls-certificate"
    assert issues[0].severity == "high"


def test_plain_http_page_is_ignored() -> None:
    assert _run(_page("http://example.com/", tls=None)) == []


@pytest.mark.parametrize("content_type", ["application/json", "image/png", "text/plain"])
def test_non_html_pages_are_ignored(content_type: str) -> None:
    page = _page("https://example.com/data", content_type=content_type, tls=None)
    assert _run(page) == []


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/xhtml+xml"])
def test_html_content_types_are_checked(content_type: str) -> None:
    page = _page(content_type=content_type, tls=None)
    assert _details(_run(page)) == [{"check": "unknown"}]


def test_non_200_page_is_ignored() -> None:
    assert _run(_page(status=404, tls=None)) == []


def test_one_issue_per_check_with_json_safe_details() -> None:
    page = _page(
        tls=_tls(
            not_after="2026-11-01T12:00:00+00:00",
            version="TLSv1",
            cipher="TLS_RSA_WITH_RC4_128_SHA",
            san=["other.example"],
        )
    )
    checks = [issue.detail["check"] for issue in _run(page)]
    assert checks == ["expired", "protocol", "san_mismatch", "weak_cipher"]
    assert len(checks) == len(set(checks))
    json.dumps(_details(_run(page)))
