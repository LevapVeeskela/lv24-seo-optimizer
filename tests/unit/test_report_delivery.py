"""Unit tests for Wave 5 report delivery (WP5.3 + WP5.4): SMTP, Telegram and idempotency."""

from __future__ import annotations

import hashlib
import json
import smtplib
from collections.abc import Iterator
from dataclasses import replace
from email import message_from_string
from email.header import decode_header, make_header
from email.message import Message
from typing import Any, ClassVar

import pytest

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.report import delivery, render
from lv24_seo.report.delivery import (
    DeliveryResult,
    EmailConfig,
    TelegramConfig,
    content_hash,
    deliver,
    send_email,
    send_telegram,
)
from lv24_seo.report.i18n import normalize_locale
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_email_text, render_html, render_subject

SendKey = tuple[str, str, str, str, str]


def _report() -> Report:
    return Report(
        kind="weekly",
        property_id="prop-1",
        period_start="2026-11-01",
        period_end="2026-11-07",
        headline="Clicks up 12% week over week",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="Clicks grew while impressions stayed flat.",
                rows=[
                    {"metric": "clicks", "significant": True, "good": True},
                    {"metric": "impressions", "significant": False, "good": False},
                ],
            )
        ],
        generated_at="2026-11-10T09:00:00Z",
        limitations=["Search Console data lags by ~2 days."],
    )


def _email_config(**overrides: Any) -> EmailConfig:
    values: dict[str, Any] = {
        "host": "smtp.example.com",
        "port": 587,
        "user": "seo@example.com",
        "password": "super-secret-password",
        "sender": "seo@example.com",
        "use_tls": True,
        "recipients": ("owner@example.com", "team@example.com"),
        "locale": "en",
    }
    values.update(overrides)
    return EmailConfig(**values)


def _telegram_config(**overrides: Any) -> TelegramConfig:
    values: dict[str, Any] = {"bot_token": "123456:token", "chat_id": "-100500", "locale": "en"}
    values.update(overrides)
    return TelegramConfig(**values)


def _expected_hash(report: Report, locale: str = "en") -> str:
    """Mirror of the semantic-hash contract: volatile envelope fields excluded.

    English omits the locale key so its digest matches the legacy ledger rows
    written before localization existed; other locales add it.
    """
    payload = report.to_dict()
    semantic: dict[str, Any] = {
        "kind": payload.get("kind"),
        "property_id": payload.get("property_id"),
        "period": payload.get("period"),
        "headline": payload.get("headline"),
        "sections": payload.get("sections"),
    }
    normalized = normalize_locale(locale)
    if normalized != "en":
        semantic["locale"] = normalized
    canonical = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _period(report: Report) -> str:
    return f"{report.period_start}..{report.period_end}"


class _FakeSMTP:
    """Minimal smtplib.SMTP stand-in that records what delivery asked it to do."""

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.tls_context: object = None
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[tuple[str, list[str], str]] = []
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self, *, context: object = None) -> None:
        self.started_tls = True
        self.tls_context = context

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def sendmail(self, sender: str, recipients: list[str], message: str) -> None:
        self.sent.append((sender, recipients, message))

    def quit(self) -> None:
        self.quit_called = True


class _RejectingSMTP(_FakeSMTP):
    def starttls(self, *, context: object = None) -> None:
        raise smtplib.SMTPAuthenticationError(
            535,
            f"bad credentials for {self.host} with super-secret-password".encode(),
        )


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeStore:
    """Duck-typed send journal; mirrors Store.record_send's boolean claim."""

    def __init__(self) -> None:
        self.sent: set[SendKey] = set()
        self.recorded: list[SendKey] = []
        self.lookups: list[SendKey] = []

    def was_sent(
        self, kind: str, property_id: str, period: str, content_hash: str, channel: str
    ) -> bool:
        key = (kind, property_id, period, content_hash, channel)
        self.lookups.append(key)
        return key in self.sent

    def record_send(
        self, kind: str, property_id: str, period: str, content_hash: str, channel: str
    ) -> bool:
        key = (kind, property_id, period, content_hash, channel)
        self.recorded.append(key)
        if key in self.sent:
            return False
        self.sent.add(key)
        return True


class _Sender:
    """Recording stand-in for send_email/send_telegram used by deliver tests."""

    def __init__(self, channel: str, *, status: str = "sent", reason: str = "") -> None:
        self.channel = channel
        self.status = status
        self.reason = reason
        self.calls = 0

    def __call__(self, report: Report, cfg: Any) -> DeliveryResult:
        self.calls += 1
        return DeliveryResult(
            channel=self.channel,
            status=self.status,
            reason=self.reason,
            content_hash=content_hash(report, getattr(cfg, "locale", "en")),
        )


def _key(report: Report, channel: str, locale: str = "en") -> SendKey:
    return (
        report.kind,
        report.property_id,
        _period(report),
        content_hash(report, locale),
        channel,
    )


def _part_text(message: Message, content_type: str) -> str:
    for part in message.walk():
        if part.get_content_type() != content_type:
            continue
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        return payload.decode(part.get_content_charset() or "utf-8")
    raise AssertionError(f"message has no {content_type} part")


def _header(message: Message, name: str) -> str:
    """Decode RFC 2047 header values so non-ASCII locales compare as plain text."""
    raw = str(message[name])
    return str(make_header(decode_header(raw)))


def _single_line(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _clean_smtp_instances() -> Iterator[None]:
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


# -- content hash -----------------------------------------------------------------------------


def test_content_hash_is_stable_and_canonical() -> None:
    report = _report()

    digest = content_hash(report)

    assert digest == _expected_hash(report)
    assert digest == content_hash(_report())
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_content_hash_changes_when_content_changes() -> None:
    report = _report()
    changed_headline = replace(report, headline="Clicks down 5% week over week")
    changed_row = replace(
        report,
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="Clicks grew while impressions stayed flat.",
                rows=[{"metric": "clicks", "significant": False}],
            )
        ],
    )

    assert content_hash(report) != content_hash(changed_headline)
    assert content_hash(report) != content_hash(changed_row)


def test_content_hash_keeps_legacy_en_and_includes_ru_zh() -> None:
    report = _report()

    en = content_hash(report, "en")
    ru = content_hash(report, "ru")
    zh = content_hash(report, "zh")

    assert len({en, ru, zh}) == 3
    assert en == _expected_hash(report, "en")
    assert ru == _expected_hash(report, "ru")
    assert zh == _expected_hash(report, "zh")
    assert content_hash(report, "EN") == en
    assert content_hash(report, "de") == en  # unsupported locales normalize to en


# -- email ------------------------------------------------------------------------------------


def test_email_builds_multipart_alternative_message() -> None:
    report = _report()

    result = send_email(report, _email_config(), smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    assert result.channel == "email"
    assert result.content_hash == content_hash(report)
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.started_tls is True
    assert smtp.logged_in == ("seo@example.com", "super-secret-password")
    assert smtp.quit_called is True

    assert len(smtp.sent) == 1
    sender, recipients, raw = smtp.sent[0]
    assert sender == "seo@example.com"
    assert recipients == ["owner@example.com", "team@example.com"]

    message = message_from_string(raw)
    assert message.get_content_type() == "multipart/alternative"
    assert _single_line(_header(message, "Subject")) == _single_line(
        render_subject(report, locale="en")
    )
    assert message["From"] == "seo@example.com"
    assert message["To"] == "owner@example.com, team@example.com"
    assert message["Content-Language"] == "en"
    assert _part_text(message, "text/plain").rstrip("\n") == render_email_text(report, locale="en")
    assert _part_text(message, "text/html").rstrip("\n") == render_html(report, locale="en")


@pytest.mark.parametrize("locale", ["en", "ru", "zh"])
def test_email_subject_and_body_use_configured_locale(
    locale: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    calls: dict[str, str] = {}

    def fake_subject(value: Report, *, locale: str = "en") -> str:
        calls["subject"] = locale
        return f"subject-{locale}"

    def fake_email_text(value: Report, *, locale: str = "en") -> str:
        calls["text"] = locale
        return f"text-{locale}"

    def fake_html(value: Report, *, title: str | None = None, locale: str = "en") -> str:
        calls["html"] = locale
        return f"<html lang='{locale}'></html>"

    monkeypatch.setattr(render, "render_subject", fake_subject, raising=False)
    monkeypatch.setattr(render, "render_email_text", fake_email_text, raising=False)
    monkeypatch.setattr(render, "render_html", fake_html, raising=False)

    result = send_email(report, _email_config(locale=locale), smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    message = message_from_string(_FakeSMTP.instances[0].sent[0][2])
    assert calls == {"subject": locale, "text": locale, "html": locale}
    assert _header(message, "Subject") == f"subject-{locale}"
    assert message["Content-Language"] == locale
    assert _part_text(message, "text/plain").rstrip("\n") == f"text-{locale}"
    assert _part_text(message, "text/html").rstrip("\n") == f"<html lang='{locale}'></html>"


@pytest.mark.parametrize("locale", ["en", "ru", "zh"])
def test_rendered_email_subject_is_single_line_and_bounded(locale: str) -> None:
    subject = render_subject(_report(), locale=locale)

    assert subject
    assert "\n" not in subject
    assert len(subject) <= 78


def test_email_header_value_preserves_nbsp_and_removes_newlines() -> None:
    assert delivery._header_value("a\nb") == "a b"
    assert delivery._header_value("1\u00a0234") == "1\u00a0234"
    assert delivery._header_value(" \n\t ") == delivery._SUBJECT_FALLBACK


@pytest.mark.parametrize("locale", ["ru", "zh"])
def test_email_localized_subject_is_a_valid_encoded_header(locale: str) -> None:
    report = _report()

    result = send_email(report, _email_config(locale=locale), smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    message = message_from_string(_FakeSMTP.instances[0].sent[0][2])
    raw = str(message["Subject"])
    decoded = _header(message, "Subject")
    assert decoded == render_subject(report, locale=locale)
    assert not decoded.isascii()  # the localized text survived the transport
    assert "\r" not in decoded
    assert "\n" not in decoded
    assert raw.isascii()  # RFC 2047 encoded, not raw 8-bit header bytes
    assert all(line.startswith((" ", "\t")) for line in raw.splitlines()[1:])


def test_email_subject_cannot_inject_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    def malicious_subject(report: Report, *, locale: str = "en") -> str:
        return "Тема\r\nBcc: attacker@example.com"

    monkeypatch.setattr(render, "render_subject", malicious_subject, raising=False)

    result = send_email(_report(), _email_config(locale="ru"), smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    raw = _FakeSMTP.instances[0].sent[0][2]
    message = message_from_string(raw)
    subject = _header(message, "Subject")
    assert message["Bcc"] is None
    assert message["To"] == "owner@example.com, team@example.com"
    assert "\r" not in subject
    assert "\n" not in subject
    assert "Bcc: attacker@example.com" in subject  # collapsed into text, not a header
    assert all(not line.lstrip().lower().startswith("bcc:") for line in raw.splitlines())


def test_email_skips_starttls_and_login_when_not_configured() -> None:
    report = _report()
    cfg = _email_config(user="", use_tls=False)

    result = send_email(report, cfg, smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    smtp = _FakeSMTP.instances[0]
    assert smtp.started_tls is False
    assert smtp.logged_in is None
    assert len(smtp.sent) == 1


def test_email_without_sender_or_recipients_fails_without_connecting() -> None:
    report = _report()

    no_recipients = send_email(report, _email_config(recipients=()), smtp_factory=_FakeSMTP)
    no_sender = send_email(report, _email_config(sender=""), smtp_factory=_FakeSMTP)

    for result in (no_recipients, no_sender):
        assert result.status == "failed"
        assert result.reason == "no_recipients"
        assert result.content_hash == content_hash(report)
    assert _FakeSMTP.instances == []


def test_email_smtp_error_reports_exception_class_only() -> None:
    report = _report()

    result = send_email(report, _email_config(), smtp_factory=_RejectingSMTP)

    assert result.status == "failed"
    assert result.reason == "SMTPAuthenticationError"
    assert "super-secret-password" not in result.reason
    assert "smtp.example.com" not in result.reason


def test_email_connection_error_reports_exception_class_only() -> None:
    report = _report()

    def broken_factory(host: str, port: int) -> Any:
        raise ConnectionRefusedError(f"connection to {host}:{port} refused")

    result = send_email(report, _email_config(), smtp_factory=broken_factory)

    assert result.status == "failed"
    assert result.reason == "ConnectionRefusedError"
    assert "smtp.example.com" not in result.reason


# -- telegram ---------------------------------------------------------------------------------


def test_telegram_sends_plain_text_truncated_to_4000_chars() -> None:
    long_rows = [{"metric": f"metric-{index}", "note": "long value " * 12} for index in range(120)]
    report = replace(
        _report(),
        headline="A very long report",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="Many rows to force truncation.",
                rows=long_rows,
            )
        ],
    )
    assert len(render_email_text(report)) > 4000
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, payload))
        return {"ok": True}

    result = send_telegram(report, _telegram_config(), http_post=fake_post)

    assert result.status == "sent"
    assert result.channel == "telegram"
    assert result.content_hash == content_hash(report, "en")
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123456:token/sendMessage"
    assert payload["chat_id"] == "-100500"
    assert "parse_mode" not in payload
    assert payload["disable_web_page_preview"] is True
    full = render_email_text(report)
    marker = delivery._truncation_marker("en")
    assert len(payload["text"]) == 4000
    assert payload["text"].endswith(marker)
    assert full.startswith(payload["text"][: 4000 - len(marker)])
    assert len(payload["text"]) == 4000


def test_telegram_truncation_marker_and_text_follow_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LocalizedTranslator:
        def __init__(self, locale: str) -> None:
            self.locale = locale

        def t(self, key: str) -> str:
            return {
                "en": "EN-MARKER",
                "ru": "RU-MARKER",
                "zh": "ZH-MARKER",
            }[self.locale]

    seen: dict[str, str] = {}

    def fake_email_text(report: Report, *, locale: str = "en") -> str:
        seen["locale"] = locale
        return "x" * 9000

    monkeypatch.setattr(delivery, "translator", _LocalizedTranslator)
    monkeypatch.setattr(render, "render_email_text", fake_email_text, raising=False)

    text = delivery._telegram_text(_report(), "ru")

    assert seen["locale"] == "ru"
    assert text.endswith("\nRU-MARKER")
    assert len(text) == delivery._TELEGRAM_LIMIT
    assert "truncated" not in text


def test_telegram_send_uses_locale_hash() -> None:
    report = _report()

    result = send_telegram(
        report,
        _telegram_config(locale="zh"),
        http_post=lambda url, payload: {"ok": True},
    )

    assert result.status == "sent"
    assert result.content_hash == content_hash(report, "zh")
    assert result.content_hash != content_hash(report, "en")


def test_telegram_http_status_error_fails() -> None:
    report = _report()

    result = send_telegram(
        report, _telegram_config(), http_post=lambda url, payload: _Response(400)
    )

    assert result.status == "failed"
    assert result.reason == "http_400"
    assert result.content_hash == content_hash(report)


def test_telegram_transport_error_fails_with_class_name() -> None:
    report = _report()

    def broken_post(url: str, payload: dict[str, Any]) -> Any:
        raise TimeoutError("api.telegram.org timed out")

    result = send_telegram(report, _telegram_config(), http_post=broken_post)

    assert result.status == "failed"
    assert result.reason == "TimeoutError"
    assert "api.telegram.org" not in result.reason


@pytest.mark.parametrize(
    "cfg",
    [
        TelegramConfig(bot_token="", chat_id="-100500"),
        TelegramConfig(bot_token="123456:token", chat_id=""),
    ],
    ids=["missing-token", "missing-chat"],
)
def test_telegram_not_configured(cfg: TelegramConfig) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    result = send_telegram(
        _report(),
        cfg,
        http_post=lambda url, payload: calls.append((url, payload)) or {"ok": True},
    )

    assert result.status == "failed"
    assert result.reason == "not_configured"
    assert calls == []


# -- deliver: channels & idempotency ----------------------------------------------------------


def test_deliver_runs_every_configured_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    email_sender = _Sender("email")
    telegram_sender = _Sender("telegram")
    monkeypatch.setattr(delivery, "send_email", email_sender)
    monkeypatch.setattr(delivery, "send_telegram", telegram_sender)

    results = deliver(
        report,
        email=_email_config(),
        telegram=_telegram_config(),
    )

    assert [result.channel for result in results] == ["email", "telegram"]
    assert [result.status for result in results] == ["sent", "sent"]
    assert email_sender.calls == 1
    assert telegram_sender.calls == 1


def test_deliver_skips_duplicate_content_via_store(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    store = _FakeStore()
    store.sent.add(_key(report, "email"))
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), store=store)

    assert results == [
        DeliveryResult(
            channel="email",
            status="skipped",
            reason="already_sent",
            content_hash=content_hash(report),
        )
    ]
    assert sender.calls == 0
    assert store.recorded == []


def test_deliver_records_send_and_then_skips_second_run(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    store = _FakeStore()
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    first = deliver(report, email=_email_config(), store=store)
    second = deliver(report, email=_email_config(), store=store)

    assert [result.status for result in first] == ["sent"]
    assert [result.status for result in second] == ["skipped"]
    assert second[0].reason == "already_sent"
    assert sender.calls == 1
    assert store.recorded == [_key(report, "email")]
    assert store.lookups == [_key(report, "email"), _key(report, "email")]


def test_deliver_uses_each_channel_config_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    store = _FakeStore()
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    # No locale= argument: the email config is the only source of truth.
    first = deliver(report, email=_email_config(locale="ru"), store=store)
    repeat = deliver(report, email=_email_config(locale="ru"), store=store)
    english = deliver(report, email=_email_config(locale="en"), store=store)

    assert first[0].content_hash == content_hash(report, "ru")
    assert [result.status for result in repeat] == ["skipped"]
    assert [result.status for result in english] == ["sent"]
    assert english[0].content_hash == content_hash(report, "en")
    assert store.recorded == [_key(report, "email", "ru"), _key(report, "email", "en")]
    assert sender.calls == 2


def test_deliver_locale_argument_is_fallback_for_config_without_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LegacyEmailConfig:
        sender = "seo@example.com"
        recipients = ("owner@example.com",)

    report = _report()
    store = _FakeStore()
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(
        report,
        email=_LegacyEmailConfig(),  # type: ignore[arg-type]
        store=store,
        locale="ru",
    )

    assert results[0].content_hash == content_hash(report, "ru")
    assert store.recorded == [_key(report, "email", "ru")]


def test_deliver_hashes_email_and_telegram_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    store = _FakeStore()
    monkeypatch.setattr(delivery, "send_email", _Sender("email"))
    monkeypatch.setattr(delivery, "send_telegram", _Sender("telegram"))

    results = deliver(
        report,
        email=_email_config(locale="ru"),
        telegram=_telegram_config(locale="zh"),
        store=store,
    )

    assert [result.content_hash for result in results] == [
        content_hash(report, "ru"),
        content_hash(report, "zh"),
    ]
    assert store.recorded == [
        _key(report, "email", "ru"),
        _key(report, "telegram", "zh"),
    ]


def test_deliver_without_store_always_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    first = deliver(report, email=_email_config())
    second = deliver(report, email=_email_config())

    assert [result.status for result in first] == ["sent"]
    assert [result.status for result in second] == ["sent"]
    assert sender.calls == 2


def test_deliver_ignores_store_when_only_on_change_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    store = _FakeStore()
    store.sent.add(_key(report, "email"))
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), store=store, only_on_change=False)

    assert [result.status for result in results] == ["sent"]
    assert sender.calls == 1


def test_deliver_does_not_record_failed_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    store = _FakeStore()
    sender = _Sender("email", status="failed", reason="SMTPAuthenticationError")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), store=store)

    assert [result.status for result in results] == ["failed"]
    assert store.recorded == []
    assert store.sent == set()


# -- deliver: material threshold --------------------------------------------------------------


def test_deliver_skips_when_material_threshold_is_not_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = replace(
        _report(),
        sections=[
            ReportSection(
                key="delta",
                title="Delta",
                summary="Nothing moved enough to matter.",
                rows=[
                    {"metric": "clicks", "significant": False},
                    {"metric": "ctr", "significant": False},
                ],
            )
        ],
    )
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), material_threshold=0.05)

    assert results == [
        DeliveryResult(
            channel="email",
            status="skipped",
            reason="no_material_change",
            content_hash=content_hash(report),
        )
    ]
    assert sender.calls == 0


def test_deliver_sends_when_material_change_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), material_threshold=0.05)

    assert [result.status for result in results] == ["sent"]
    assert sender.calls == 1


def test_material_gate_can_suppress_a_locale_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    report = replace(
        _report(),
        sections=[
            ReportSection(
                key="delta",
                title="Delta",
                summary="Nothing moved enough to matter.",
                rows=[{"metric": "clicks", "significant": False}],
            )
        ],
    )
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)
    thresholds = {"clicks_pct": 10.0}

    russian = deliver(report, email=_email_config(locale="ru"), material_thresholds=thresholds)
    english = deliver(report, email=_email_config(locale="en"), material_thresholds=thresholds)

    assert [result.reason for result in russian] == ["no_material_change"]
    assert [result.reason for result in english] == ["no_material_change"]
    assert russian[0].content_hash == content_hash(report, "ru")
    assert english[0].content_hash == content_hash(report, "en")
    assert sender.calls == 0


def test_deliver_zero_threshold_disables_material_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    report = replace(
        _report(),
        sections=[
            ReportSection(
                key="delta",
                title="Delta",
                summary="Flat.",
                rows=[{"metric": "clicks", "significant": False}],
            )
        ],
    )
    sender = _Sender("email")
    monkeypatch.setattr(delivery, "send_email", sender)

    results = deliver(report, email=_email_config(), material_threshold=0.0)

    assert [result.status for result in results] == ["sent"]
    assert sender.calls == 1


def test_deliver_rejects_negative_material_threshold() -> None:
    with pytest.raises(SeoError) as excinfo:
        deliver(_report(), email=_email_config(), material_threshold=-0.1)

    assert excinfo.value.code == INVALID_INPUT
