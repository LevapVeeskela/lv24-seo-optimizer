"""Unit tests for Wave 5 report delivery (WP5.3 + WP5.4): SMTP, Telegram and idempotency."""

from __future__ import annotations

import hashlib
import json
import smtplib
from collections.abc import Iterator
from dataclasses import replace
from email import message_from_string
from email.message import Message
from typing import Any, ClassVar

import pytest

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.report import delivery
from lv24_seo.report.delivery import (
    DeliveryResult,
    EmailConfig,
    TelegramConfig,
    content_hash,
    deliver,
    send_email,
    send_telegram,
)
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_html, render_markdown, render_text

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
    }
    values.update(overrides)
    return EmailConfig(**values)


def _telegram_config(**overrides: Any) -> TelegramConfig:
    values: dict[str, Any] = {"bot_token": "123456:token", "chat_id": "-100500"}
    values.update(overrides)
    return TelegramConfig(**values)


def _expected_hash(report: Report) -> str:
    canonical = json.dumps(
        report.to_dict(),
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
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[tuple[str, list[str], str]] = []
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def sendmail(self, sender: str, recipients: list[str], message: str) -> None:
        self.sent.append((sender, recipients, message))

    def quit(self) -> None:
        self.quit_called = True


class _RejectingSMTP(_FakeSMTP):
    def starttls(self) -> None:
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
            content_hash=content_hash(report),
        )


def _key(report: Report, channel: str) -> SendKey:
    return (
        report.kind,
        report.property_id,
        _period(report),
        content_hash(report),
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
    assert message["Subject"] == "[SEO] Clicks up 12% week over week"
    assert message["From"] == "seo@example.com"
    assert message["To"] == "owner@example.com, team@example.com"
    assert _part_text(message, "text/plain").rstrip("\n") == render_text(report)
    assert _part_text(message, "text/html").rstrip("\n") == render_html(report)


def test_email_subject_truncates_headline_to_120_chars() -> None:
    headline = "Weekly traffic report " * 20
    report = replace(_report(), headline=headline)

    result = send_email(report, _email_config(), smtp_factory=_FakeSMTP)

    assert result.status == "sent"
    message = message_from_string(_FakeSMTP.instances[0].sent[0][2])
    expected = "[SEO] " + " ".join(headline.split())[:120]
    # Long headers are folded by the generator; unfolding removes only the line break.
    subject = str(message["Subject"]).replace("\n", "")
    assert subject == expected
    assert len(subject) == len("[SEO] ") + 120


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


def test_telegram_sends_markdown_truncated_to_4000_chars() -> None:
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
    assert len(render_markdown(report)) > 4000
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, payload))
        return {"ok": True}

    result = send_telegram(report, _telegram_config(), http_post=fake_post)

    assert result.status == "sent"
    assert result.channel == "telegram"
    assert result.content_hash == content_hash(report)
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123456:token/sendMessage"
    assert payload["chat_id"] == "-100500"
    assert payload["parse_mode"] == "Markdown"
    assert payload["text"] == render_markdown(report)[:4000]
    assert len(payload["text"]) == 4000


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
