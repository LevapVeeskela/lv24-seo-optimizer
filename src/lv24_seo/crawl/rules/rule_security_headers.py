"""``security-headers``: baseline HTTP security headers and cookie flags.

The rule reads only what the fetcher already captured — response headers and ``Set-Cookie``
values — and never issues requests of its own. It applies to status-200 HTML pages, because
the headers protect documents rather than assets. Every finding is a separate issue so a
report can be filtered per header, and a page is capped at
:data:`_MAX_ISSUES_PER_PAGE` issues so a response with many weak cookies cannot flood it.

Cookie expiry uses the page's ``fetched_at`` timestamp (captured by the fetcher) instead of
the rule-time clock, so a crawl stays deterministic and the comparison can be tested with
fixed dates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "security-headers"
_SEVERITY: Severity = "medium"

# 180 days, the minimum recommended by OWASP and required by the preload list.
_HSTS_MIN_MAX_AGE = 15_552_000
_MAX_ISSUES_PER_PAGE = 20
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_CSP_UNSAFE_TOKENS = ("unsafe-inline", "unsafe-eval")


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _fetched_at(page: PageData) -> datetime | None:
    """Parse the crawl timestamp; a missing or malformed value disables expiry filtering."""
    if not page.fetched_at:
        return None
    try:
        parsed = datetime.fromisoformat(page.fetched_at)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in _HTML_TYPES


def _is_https(page: PageData) -> bool:
    # Response headers belong to the final URL after redirects.
    return urlsplit(page.final_url or page.url).scheme.lower() == "https"


def _headers_lower(page: PageData) -> dict[str, str]:
    """Header lookup table with lower-cased names, as HTTP header names are case-insensitive."""
    return {name.lower(): value for name, value in page.headers.items()}


def _hsts_max_age(value: str) -> int | None:
    for part in value.split(";"):
        key, _, raw = part.partition("=")
        if key.strip().lower() != "max-age":
            continue
        try:
            return int(raw.strip().strip('"'))
        except ValueError:
            return None
    return None


def _parse_csp(value: str) -> dict[str, list[str]]:
    """Map directive names to lower-cased source expressions.

    Repeated names are merged rather than overwritten so an unsafe token in a
    duplicate directive cannot hide from the check.
    """
    directives: dict[str, list[str]] = {}
    for directive in value.split(";"):
        tokens = directive.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        directives.setdefault(name, []).extend(token.lower() for token in tokens[1:])
    return directives


def _csp_unsafe_tokens(
    directives: dict[str, list[str]],
    *,
    allow_style_attr_inline: bool,
) -> list[str]:
    """Unsafe tokens found in the policy, in the stable report order.

    ``style-src-attr 'unsafe-inline'`` is the only tolerated form when the
    caller opts in; ``unsafe-eval`` and every other directive stay reportable.
    """
    found: set[str] = set()
    for name, sources in directives.items():
        for source in sources:
            keyword = source.strip("'\"")
            if keyword not in _CSP_UNSAFE_TOKENS:
                continue
            if allow_style_attr_inline and keyword == "unsafe-inline" and name == "style-src-attr":
                continue
            found.add(keyword)
    return [keyword for keyword in _CSP_UNSAFE_TOKENS if keyword in found]


def _parse_cookie(raw: str) -> tuple[str, dict[str, str]] | None:
    """Split one ``Set-Cookie`` value into its name and lower-cased attributes.

    Returns ``None`` for strings without a usable ``name=value`` pair instead of raising:
    a malformed header is not a reason to fail the whole crawl.
    """
    parts = raw.split(";")
    if "=" not in parts[0]:
        return None
    name = parts[0].split("=", 1)[0].strip()
    if not name:
        return None
    attributes: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            attributes[key.strip().lower()] = value.strip()
        else:
            attributes[part.strip().lower()] = ""
    return name, attributes


def _is_expired(attributes: dict[str, str], reference: datetime | None) -> bool:
    """``Max-Age`` wins over ``Expires`` (RFC 6265); unparseable dates are not expired."""
    max_age = attributes.get("max-age")
    if max_age is not None:
        try:
            return int(max_age.strip()) <= 0
        except ValueError:
            pass
    expires = attributes.get("expires")
    if expires and reference is not None:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when <= reference
    return False


def _header_issues(
    page: PageData,
    headers: dict[str, str],
    *,
    allow_style_attr_inline: bool,
) -> list[Issue]:
    issues: list[Issue] = []

    if _is_https(page):
        hsts = headers.get("strict-transport-security", "").strip()
        if not hsts:
            issues.append(
                _issue(
                    page.url,
                    {"header": "strict-transport-security", "status": "missing", "max_age": None},
                )
            )
        else:
            max_age = _hsts_max_age(hsts)
            if max_age is None or max_age < _HSTS_MIN_MAX_AGE:
                issues.append(
                    _issue(
                        page.url,
                        {
                            "header": "strict-transport-security",
                            "status": "weak",
                            "max_age": max_age,
                        },
                    )
                )

    csp = headers.get("content-security-policy", "").strip()
    csp_directives: set[str] = set()
    if not csp:
        issues.append(_issue(page.url, {"header": "content-security-policy", "status": "missing"}))
    else:
        directives = _parse_csp(csp)
        csp_directives = set(directives)
        unsafe = _csp_unsafe_tokens(
            directives,
            allow_style_attr_inline=allow_style_attr_inline,
        )
        if unsafe:
            issues.append(
                _issue(
                    page.url,
                    {"header": "content-security-policy", "status": "weak", "directives": unsafe},
                )
            )

    xfo = headers.get("x-frame-options", "").strip()
    if not xfo and "frame-ancestors" not in csp_directives:
        issues.append(_issue(page.url, {"header": "x-frame-options", "status": "missing"}))

    xcto = headers.get("x-content-type-options", "").strip()
    if not xcto:
        issues.append(_issue(page.url, {"header": "x-content-type-options", "status": "missing"}))
    elif xcto.lower() != "nosniff":
        issues.append(
            _issue(
                page.url,
                {"header": "x-content-type-options", "status": "weak", "value": xcto},
            )
        )

    referrer = headers.get("referrer-policy", "").strip()
    if not referrer:
        issues.append(_issue(page.url, {"header": "referrer-policy", "status": "missing"}))
    else:
        values = {part.strip().lower() for part in referrer.split(",")}
        if "unsafe-url" in values:
            issues.append(
                _issue(
                    page.url,
                    {"header": "referrer-policy", "status": "weak", "value": referrer},
                )
            )

    if not headers.get("permissions-policy", "").strip():
        issues.append(_issue(page.url, {"header": "permissions-policy", "status": "missing"}))

    return issues


def _cookie_issues(page: PageData) -> list[Issue]:
    https = _is_https(page)
    reference = _fetched_at(page)
    issues: list[Issue] = []
    for raw in page.set_cookie:
        if len(issues) >= _MAX_ISSUES_PER_PAGE:
            break
        parsed = _parse_cookie(raw)
        if parsed is None:
            continue
        name, attributes = parsed
        if _is_expired(attributes, reference):
            continue
        missing: list[str] = []
        if https and "secure" not in attributes:
            missing.append("secure")
        if "httponly" not in attributes:
            missing.append("httponly")
        same_site = attributes.get("samesite", "").strip().lower()
        if same_site == "none" and "secure" not in attributes and "secure" not in missing:
            missing.append("secure")
        if not missing:
            continue
        # The cookie value is deliberately absent: findings must never leak session material.
        detail: dict[str, Any] = {
            "header": "set-cookie",
            "cookie": name,
            "status": "weak",
            "missing": missing,
        }
        if same_site == "none":
            detail["samesite"] = same_site
        issues.append(_issue(page.url, detail))
    return issues


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        page_issues = _header_issues(
            page,
            _headers_lower(page),
            allow_style_attr_inline=ctx.csp_allow_style_attr_inline,
        )
        page_issues.extend(_cookie_issues(page))
        issues.extend(page_issues[:_MAX_ISSUES_PER_PAGE])
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Missing or weak HTTP security headers",
    description=(
        "HTML pages should send HSTS, a Content-Security-Policy without unsafe-inline or "
        "unsafe-eval (unless the documented style-src-attr exception is enabled), framing "
        "protection, nosniff, a safe Referrer-Policy and a Permissions-Policy; cookies should "
        "be Secure, HttpOnly and never SameSite=None without Secure."
    ),
    check=_check,
)
