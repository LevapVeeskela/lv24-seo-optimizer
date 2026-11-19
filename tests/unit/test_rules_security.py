"""Unit tests for the ``security-headers`` rule (offline, synthetic pages only)."""

from __future__ import annotations

import json

from lv24_seo.crawl.models import Issue, PageData, RuleContext
from lv24_seo.crawl.rules import load_rules, rule_security_headers

_RULE = rule_security_headers.RULE
_CLEAN_COOKIE = "sid=abc123; Path=/; Secure; HttpOnly; SameSite=Lax"


def _clean_headers() -> dict[str, str]:
    return {
        "strict-transport-security": "max-age=31536000; includeSubDomains",
        "content-security-policy": "default-src 'self'",
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": "geolocation=()",
    }


def _page(
    url: str = "https://example.com/",
    *,
    status: int = 200,
    content_type: str = "text/html",
    headers: dict[str, str] | None = None,
    set_cookie: list[str] | None = None,
    fetched_at: str | None = "2026-11-18T12:00:00+00:00",
) -> PageData:
    return PageData(
        url=url,
        status=status,
        final_url=url,
        content_type=content_type,
        headers=_clean_headers() if headers is None else headers,
        set_cookie=[] if set_cookie is None else set_cookie,
        fetched_at=fetched_at,
    )


def _ctx(*pages: PageData, csp_allow_style_attr_inline: bool = False) -> RuleContext:
    return RuleContext(
        pages=list(pages),
        site_url="https://example.com/",
        csp_allow_style_attr_inline=csp_allow_style_attr_inline,
    )


def _find(issues: list[Issue], header: str, status: str | None = None) -> list[Issue]:
    return [
        issue
        for issue in issues
        if issue.detail.get("header") == header
        and (status is None or issue.detail.get("status") == status)
    ]


def test_rule_contract_and_discovery() -> None:
    assert _RULE.id == "security-headers"
    assert _RULE.severity == "medium"
    assert "security-headers" in {rule.id for rule in load_rules()}


def test_clean_page_and_cookie_produce_no_issues() -> None:
    page = _page(set_cookie=[_CLEAN_COOKIE])

    assert _RULE.run(_ctx(page)) == []


def test_header_lookup_is_case_insensitive() -> None:
    headers = {name.upper(): value for name, value in _clean_headers().items()}

    assert _RULE.run(_ctx(_page(headers=headers))) == []


# --- Strict-Transport-Security ---------------------------------------------


def test_hsts_missing_on_https() -> None:
    headers = _clean_headers()
    del headers["strict-transport-security"]

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert len(issues) == 1
    assert issues[0].detail == {
        "header": "strict-transport-security",
        "status": "missing",
        "max_age": None,
    }


def test_hsts_max_age_boundary_is_accepted() -> None:
    headers = _clean_headers()
    headers["strict-transport-security"] = "max-age=15552000"

    assert _RULE.run(_ctx(_page(headers=headers))) == []


def test_hsts_max_age_below_boundary_is_weak() -> None:
    headers = _clean_headers()
    headers["strict-transport-security"] = "max-age=15551999"

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert len(issues) == 1
    assert issues[0].detail == {
        "header": "strict-transport-security",
        "status": "weak",
        "max_age": 15_551_999,
    }


def test_hsts_without_max_age_is_weak() -> None:
    headers = _clean_headers()
    headers["strict-transport-security"] = "includeSubDomains"

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert len(issues) == 1
    assert issues[0].detail["status"] == "weak"
    assert issues[0].detail["max_age"] is None


def test_hsts_ignored_on_plain_http_page() -> None:
    page = _page("http://example.com/", headers={})

    issues = _RULE.run(_ctx(page))

    assert _find(issues, "strict-transport-security") == []
    assert _find(issues, "content-security-policy", "missing")


# --- Content-Security-Policy and framing -----------------------------------


def test_csp_missing() -> None:
    headers = _clean_headers()
    del headers["content-security-policy"]

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert _find(issues, "content-security-policy")[0].detail == {
        "header": "content-security-policy",
        "status": "missing",
    }


def test_csp_unsafe_tokens_are_reported_in_stable_order() -> None:
    headers = _clean_headers()
    headers["content-security-policy"] = "script-src 'unsafe-eval' 'unsafe-inline'"

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert len(issues) == 1
    assert issues[0].detail == {
        "header": "content-security-policy",
        "status": "weak",
        "directives": ["unsafe-inline", "unsafe-eval"],
    }


_LV24_CSP = "default-src 'self'; style-src 'self' 'nonce-abc'; style-src-attr 'unsafe-inline'"


def _csp_page(csp: str) -> PageData:
    headers = _clean_headers()
    headers["content-security-policy"] = csp
    return _page(headers=headers)


def test_style_attr_unsafe_inline_is_weak_by_default() -> None:
    issues = _RULE.run(_ctx(_csp_page(_LV24_CSP)))

    assert _find(issues, "content-security-policy")[0].detail == {
        "header": "content-security-policy",
        "status": "weak",
        "directives": ["unsafe-inline"],
    }


def test_style_attr_unsafe_inline_exception_is_opt_in() -> None:
    assert _RULE.run(_ctx(_csp_page(_LV24_CSP), csp_allow_style_attr_inline=True)) == []


def test_style_attr_exception_does_not_hide_script_src_unsafe_inline() -> None:
    csp = f"{_LV24_CSP}; script-src 'unsafe-inline'"

    issues = _RULE.run(_ctx(_csp_page(csp), csp_allow_style_attr_inline=True))

    assert _find(issues, "content-security-policy")[0].detail["directives"] == ["unsafe-inline"]


def test_style_attr_exception_does_not_hide_style_src_unsafe_inline() -> None:
    csp = "default-src 'self'; style-src 'self' 'unsafe-inline'; style-src-attr 'unsafe-inline'"

    issues = _RULE.run(_ctx(_csp_page(csp), csp_allow_style_attr_inline=True))

    assert _find(issues, "content-security-policy")[0].detail["directives"] == ["unsafe-inline"]


def test_style_attr_exception_never_hides_unsafe_eval() -> None:
    csp = "default-src 'self'; style-src 'self'; style-src-attr 'unsafe-eval'"

    issues = _RULE.run(_ctx(_csp_page(csp), csp_allow_style_attr_inline=True))

    assert _find(issues, "content-security-policy")[0].detail["directives"] == ["unsafe-eval"]


def test_csp_frame_ancestors_satisfies_x_frame_options() -> None:
    headers = _clean_headers()
    headers["content-security-policy"] = "default-src 'self'; frame-ancestors 'none'"
    del headers["x-frame-options"]

    assert _RULE.run(_ctx(_page(headers=headers))) == []


def test_x_frame_options_alone_satisfies_framing() -> None:
    headers = _clean_headers()
    headers["content-security-policy"] = "default-src 'self'"
    headers["x-frame-options"] = "SAMEORIGIN"

    assert _RULE.run(_ctx(_page(headers=headers))) == []


def test_x_frame_options_missing_without_frame_ancestors() -> None:
    headers = _clean_headers()
    del headers["x-frame-options"]

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert _find(issues, "x-frame-options")[0].detail == {
        "header": "x-frame-options",
        "status": "missing",
    }


# --- X-Content-Type-Options, Referrer-Policy, Permissions-Policy -----------


def test_x_content_type_options_missing_and_weak() -> None:
    missing_headers = _clean_headers()
    del missing_headers["x-content-type-options"]
    weak_headers = _clean_headers()
    weak_headers["x-content-type-options"] = "sniff"

    missing = _RULE.run(_ctx(_page(headers=missing_headers)))
    weak = _RULE.run(_ctx(_page(headers=weak_headers)))

    assert _find(missing, "x-content-type-options")[0].detail["status"] == "missing"
    assert _find(weak, "x-content-type-options")[0].detail == {
        "header": "x-content-type-options",
        "status": "weak",
        "value": "sniff",
    }


def test_x_content_type_options_nosniff_is_case_insensitive() -> None:
    headers = _clean_headers()
    headers["x-content-type-options"] = "NoSniff"

    assert _RULE.run(_ctx(_page(headers=headers))) == []


def test_referrer_policy_missing_and_unsafe_url() -> None:
    missing_headers = _clean_headers()
    del missing_headers["referrer-policy"]
    unsafe_headers = _clean_headers()
    unsafe_headers["referrer-policy"] = "unsafe-url"

    missing = _RULE.run(_ctx(_page(headers=missing_headers)))
    unsafe = _RULE.run(_ctx(_page(headers=unsafe_headers)))

    assert _find(missing, "referrer-policy")[0].detail["status"] == "missing"
    assert _find(unsafe, "referrer-policy")[0].detail == {
        "header": "referrer-policy",
        "status": "weak",
        "value": "unsafe-url",
    }


def test_permissions_policy_missing() -> None:
    headers = _clean_headers()
    del headers["permissions-policy"]

    issues = _RULE.run(_ctx(_page(headers=headers)))

    assert _find(issues, "permissions-policy")[0].detail == {
        "header": "permissions-policy",
        "status": "missing",
    }


# --- Cookies ----------------------------------------------------------------


def test_cookie_missing_secure_and_httponly_is_one_issue() -> None:
    issues = _RULE.run(_ctx(_page(set_cookie=["sid=abc123; Path=/"])))

    assert len(issues) == 1
    assert issues[0].detail == {
        "header": "set-cookie",
        "cookie": "sid",
        "status": "weak",
        "missing": ["secure", "httponly"],
    }
    assert "abc123" not in json.dumps(issues[0].detail)


def test_cookie_samesite_none_without_secure() -> None:
    issues = _RULE.run(_ctx(_page(set_cookie=["sid=abc; HttpOnly; SameSite=None"])))

    assert len(issues) == 1
    assert issues[0].detail["missing"] == ["secure"]
    assert issues[0].detail["samesite"] == "none"


def test_cookie_samesite_none_with_secure_but_no_httponly() -> None:
    issues = _RULE.run(_ctx(_page(set_cookie=["sid=abc; Secure; SameSite=None"])))

    assert len(issues) == 1
    assert issues[0].detail["missing"] == ["httponly"]


def test_expired_cookies_are_ignored() -> None:
    cookies = [
        "old=1; Max-Age=0",
        "gone=1; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ]

    assert _RULE.run(_ctx(_page(set_cookie=cookies))) == []


def test_expiry_needs_the_crawl_timestamp() -> None:
    # Without fetched_at the rule cannot prove the cookie expired, so it stays conservative
    # and reports the missing flags instead of silently skipping it.
    page = _page(
        set_cookie=["gone=1; Expires=Thu, 01 Jan 1970 00:00:00 GMT"],
        fetched_at=None,
    )

    issues = _RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail["cookie"] == "gone"


def test_future_expiry_keeps_cookie_checks() -> None:
    cookies = ["sid=abc; Secure; HttpOnly; SameSite=Lax; Expires=Fri, 01 Jan 2100 00:00:00 GMT"]

    assert _RULE.run(_ctx(_page(set_cookie=cookies))) == []


def test_cookie_secure_flag_is_only_required_on_https() -> None:
    page = _page("http://example.com/", headers={}, set_cookie=["sid=abc; HttpOnly"])

    issues = _RULE.run(_ctx(page))

    assert _find(issues, "set-cookie") == []


def test_malformed_cookie_strings_do_not_crash() -> None:
    cookies = ["", "; ;", "no-equals-here", "=value; Secure", _CLEAN_COOKIE]

    assert _RULE.run(_ctx(_page(set_cookie=cookies))) == []


def test_cookie_issue_count_is_bounded() -> None:
    cookies = [f"cookie{index}=value" for index in range(25)]

    issues = _RULE.run(_ctx(_page(set_cookie=cookies)))

    assert len(issues) == 20


# --- Page selection ---------------------------------------------------------


def test_non_html_pages_are_skipped() -> None:
    image = _page("https://example.com/cover.png", content_type="image/png", headers={})
    data = _page("https://example.com/data.json", content_type="application/json", headers={})

    assert _RULE.run(_ctx(image, data)) == []


def test_xhtml_pages_are_checked() -> None:
    page = _page(content_type="application/xhtml+xml", headers={})

    assert len(_RULE.run(_ctx(page))) == 6


def test_non_200_pages_are_skipped() -> None:
    pages = [
        _page("https://example.com/old", status=301, headers={}),
        _page("https://example.com/missing", status=404, headers={}),
    ]

    assert _RULE.run(_ctx(*pages)) == []


def test_detail_is_json_serialisable() -> None:
    page = _page(
        headers={},
        set_cookie=["sid=abc", "pref=1; Secure"],
    )

    issues = _RULE.run(_ctx(page))

    assert issues
    assert json.loads(json.dumps([issue.detail for issue in issues])) == [
        issue.detail for issue in issues
    ]
