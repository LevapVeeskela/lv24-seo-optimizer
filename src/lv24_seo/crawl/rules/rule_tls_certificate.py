"""``tls-certificate``: HTTPS pages must serve a current, modern certificate.

The fetcher records the negotiated certificate in :attr:`PageData.tls`; this rule only
reads those facts and never opens a connection. One page can produce several findings,
but never more than one per check:

* expiry (``expired`` / ``expiring`` / ``expiring_soon`` / ``unknown_expiry``),
* protocol version (``protocol`` for TLS 1.0 and 1.1),
* hostname coverage (``san_mismatch`` / ``unknown_san``),
* cipher suite (``weak_cipher`` for RC4, 3DES, NULL and EXPORT suites).

A page whose certificate could not be inspected gets a single ``unknown`` finding
instead of a silent pass, because the report must not claim coverage it does not have.
Pages that are not HTTPS, not status 200 or not HTML are skipped: plain-HTTP downgrades
and mixed content are reported by their own rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "tls-certificate"
_SEVERITY: Severity = "high"
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_EXPIRING_DAYS = 14
_EXPIRING_SOON_DAYS = 30
_MAX_SAN = 20
_WEAK_CIPHER_MARKERS = ("rc4", "3des", "null", "export")
_WEAK_PROTOCOLS = frozenset({"tlsv1", "tlsv1.1"})


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _utcnow() -> datetime:
    """Current UTC time; isolated so tests can freeze it without patching ``datetime``."""
    return datetime.now(UTC)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in _HTML_TYPES


def _host_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.rstrip(".").lower()


def _parse_not_after(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _expiry_issue(url: str, value: object, now: datetime) -> Issue | None:
    not_after = _parse_not_after(value)
    if not_after is None:
        return _issue(url, {"check": "unknown_expiry"})
    days_left = (not_after - now).days
    if not_after <= now:
        check = "expired"
    elif days_left < _EXPIRING_DAYS:
        check = "expiring"
    elif days_left < _EXPIRING_SOON_DAYS:
        check = "expiring_soon"
    else:
        return None
    reported = value if isinstance(value, str) else not_after.isoformat()
    return _issue(url, {"check": check, "not_after": reported, "days_left": days_left})


def _protocol_issue(url: str, version: object) -> Issue | None:
    if not isinstance(version, str):
        return None
    if version.strip().lower() in _WEAK_PROTOCOLS:
        return _issue(url, {"check": "protocol", "version": version})
    return None


def _san_entries(tls: dict[str, Any]) -> list[str]:
    raw = tls.get("san")
    if not isinstance(raw, (list, tuple)):
        return []
    return [entry.strip() for entry in raw if isinstance(entry, str) and entry.strip()]


def _common_name(tls: dict[str, Any]) -> str | None:
    subject = tls.get("subject")
    if not isinstance(subject, dict):
        return None
    common_name = subject.get("commonName")
    if isinstance(common_name, str) and common_name.strip():
        return common_name.strip()
    return None


def _hostname_matches(host: str, pattern: str) -> bool:
    """Match a host against one SAN/CN entry, honouring a single-label wildcard."""
    candidate = pattern.strip().lower().rstrip(".")
    if not candidate:
        return False
    if candidate.startswith("*."):
        suffix = candidate[2:]
        if not suffix or not host.endswith(f".{suffix}"):
            return False
        label = host[: -(len(suffix) + 1)]
        return bool(label) and "." not in label
    return host == candidate


def _hostname_issue(url: str, tls: dict[str, Any]) -> Issue | None:
    host = _host_of(url)
    if not host:
        return None
    san = _san_entries(tls)
    common_name = _common_name(tls)
    candidates = san if san else ([common_name] if common_name else [])
    if not candidates:
        return _issue(url, {"check": "unknown_san"})
    if any(_hostname_matches(host, name) for name in candidates):
        return None
    return _issue(url, {"check": "san_mismatch", "host": host, "san": san[:_MAX_SAN]})


def _cipher_issue(url: str, cipher: object) -> Issue | None:
    if not isinstance(cipher, str) or not cipher.strip():
        return None
    lowered = cipher.lower()
    if any(marker in lowered for marker in _WEAK_CIPHER_MARKERS):
        return _issue(url, {"check": "weak_cipher", "cipher": cipher})
    return None


def _page_issues(page: PageData, now: datetime) -> list[Issue]:
    if not _is_html_page(page):
        return []
    effective_url = page.final_url or page.url
    if urlsplit(effective_url).scheme.lower() != "https":
        return []
    tls = page.tls
    if tls is None:
        return [_issue(page.url, {"check": "unknown"})]
    findings = [
        _expiry_issue(page.url, tls.get("not_after"), now),
        _protocol_issue(page.url, tls.get("version")),
        _hostname_issue(effective_url, tls),
        _cipher_issue(page.url, tls.get("cipher")),
    ]
    return [finding for finding in findings if finding is not None]


def _check(ctx: RuleContext) -> list[Issue]:
    now = _utcnow()
    issues: list[Issue] = []
    for page in ctx.pages:
        issues.extend(_page_issues(page, now))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="TLS certificate problems",
    description=(
        "An HTTPS page must present a certificate that is valid for its host, not expired "
        "or close to expiry, negotiated over TLS 1.2+ with a modern cipher suite."
    ),
    check=_check,
)
