"""Regression tests for the F07-F09 delivery fixes.

All transports are faked (no real SMTP, no real Telegram calls); time and the
send journal use in-memory doubles.
"""

from __future__ import annotations

import smtplib
import ssl
from typing import Any, ClassVar

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


def _report(*, generated_at: str = "2026-11-11T10:00:00+00:00") -> Report:
    return Report(
        kind="weekly",
        property_id="https://example.com/",
        period_start="2026-11-02",
        period_end="2026-11-09",
        headline="clicks +12%",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="clicks +12%",
                rows=[{"query": "seo", "clicks_change_pct": 12.0}],
            )
        ],
        generated_at=generated_at,
        limitations=["gsc only"],
        warnings=["degraded"],
    )


class _VerifyingSMTP:
    """Fake SMTP that records the STARTTLS context and refuses unverified use."""

    instances: ClassVar[list[_VerifyingSMTP]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.context: Any = None
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[tuple[str, list[str], str]] = []
        _VerifyingSMTP.instances.append(self)

    def starttls(self, *, context: Any = None) -> None:
        self.context = context
        if context is None or getattr(context, "verify_mode", None) != ssl.CERT_REQUIRED:
            raise smtplib.SMTPException("unverified context")

    def login(self, user: str, password: str) -> None:
        if self.context is None:
            raise smtplib.SMTPException("login before verified TLS")
        self.logged_in = (user, password)

    def sendmail(self, sender: str, recipients: list[str], message: str) -> None:
        self.sent.append((sender, recipients, message))

    def quit(self) -> None:
        pass


def _email_cfg() -> EmailConfig:
    return EmailConfig(
        host="smtp.example.com",
        user="bot",
        password="secret",
        sender="bot@example.com",
        recipients=("owner@example.com",),
    )


# --- F07: verified TLS ----------------------------------------------------------------


def test_email_uses_verifying_tls_context() -> None:
    _VerifyingSMTP.instances.clear()
    result = send_email(_report(), _email_cfg(), smtp_factory=_VerifyingSMTP)

    assert result.status == "sent"
    context = _VerifyingSMTP.instances[-1].context
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_email_login_never_runs_after_tls_failure() -> None:
    class _FailingTLS(_VerifyingSMTP):
        def starttls(self, *, context: Any = None) -> None:
            raise smtplib.SMTPException("handshake failed")

    result = send_email(_report(), _email_cfg(), smtp_factory=_FailingTLS)

    assert result.status == "failed"
    assert _FailingTLS.instances[-1].logged_in is None


# --- F08: Telegram transport normalization --------------------------------------------


def _telegram_cfg() -> TelegramConfig:
    return TelegramConfig(bot_token="token", chat_id="123")


def test_telegram_int_status_codes_are_not_sent() -> None:
    assert send_telegram(_report(), _telegram_cfg(), http_post=lambda _u, _p: 200).status == "sent"
    assert (
        send_telegram(_report(), _telegram_cfg(), http_post=lambda _u, _p: 400).status == "failed"
    )
    failed = send_telegram(_report(), _telegram_cfg(), http_post=lambda _u, _p: 429)
    assert failed.status == "failed"
    assert failed.reason == "http_429"


def test_telegram_bot_api_error_dict_is_not_sent() -> None:
    payload = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    result = send_telegram(_report(), _telegram_cfg(), http_post=lambda _u, _p: dict(payload))
    assert result.status == "failed"
    assert result.reason == "http_400"


def test_telegram_timeout_is_failed_not_sent() -> None:
    def _boom(_url: str, _payload: dict[str, Any]) -> None:
        raise TimeoutError("timed out")

    result = send_telegram(_report(), _telegram_cfg(), http_post=_boom)
    assert result.status == "failed"
    assert result.reason == "TimeoutError"


# --- F09: semantic hash, material gate, duplicate race --------------------------------


class _Store:
    def __init__(self) -> None:
        self.sent: set[tuple[str, ...]] = set()

    def was_sent(self, *key: str) -> bool:
        return key in self.sent

    def record_send(self, *key: str) -> bool:
        if key in self.sent:
            return False
        self.sent.add(key)
        return True


def _patched_send(monkeypatch: Any, status: str = "sent") -> None:
    """Route deliver() through a fake sender (no network)."""
    import lv24_seo.report.delivery as delivery_module

    def _fake_send(report: Report, cfg: Any, **kwargs: Any) -> delivery_module.DeliveryResult:
        return delivery_module.DeliveryResult(
            channel="telegram", status=status, content_hash=content_hash(report)
        )

    monkeypatch.setattr(delivery_module, "send_telegram", _fake_send)


def test_content_hash_ignores_generated_at_and_diagnostics() -> None:
    assert content_hash(_report(generated_at="2026-11-11T10:00:00+00:00")) == content_hash(
        _report(generated_at="2026-11-12T10:00:00+00:00")
    )


def test_only_on_change_skips_rebuilt_report(monkeypatch: Any) -> None:
    _patched_send(monkeypatch)
    store = _Store()
    first = deliver(_report(), telegram=_telegram_cfg(), store=store)
    assert all(r.status == "sent" for r in first)

    rebuilt = deliver(
        _report(generated_at="2026-11-12T10:00:00+00:00"),
        telegram=_telegram_cfg(),
        store=store,
    )
    assert [r.status for r in rebuilt] == ["skipped"]
    assert rebuilt[0].reason == "already_sent"


def test_material_gate_blocks_small_but_passes_significant_change(monkeypatch: Any) -> None:
    _patched_send(monkeypatch)
    small = _report()
    small.sections[0].rows = [{"query": "seo", "clicks_change_pct": 2.0}]
    skipped = deliver(small, telegram=_telegram_cfg(), material_thresholds={"clicks_pct": 10.0})
    assert [r.reason for r in skipped] == ["no_material_change"]

    big = _report()
    big.sections[0].rows = [{"query": "seo", "clicks_change_pct": 12.0}]
    sent = deliver(big, telegram=_telegram_cfg(), material_thresholds={"clicks_pct": 10.0})
    assert [r.status for r in sent] == ["sent"]

    flagged = _report()
    flagged.sections[0].rows = [{"query": "seo", "significant": True}]
    sent_flag = deliver(flagged, telegram=_telegram_cfg(), material_thresholds={"clicks_pct": 10.0})
    assert [r.status for r in sent_flag] == ["sent"]


def test_duplicate_race_keeps_sent_with_race_note(monkeypatch: Any) -> None:
    import lv24_seo.report.delivery as delivery_module

    store = _Store()
    digest_key = (
        "weekly",
        "https://example.com/",
        "2026-11-02..2026-11-09",
        content_hash(_report()),
        "telegram",
    )

    def _fake_send(report: Report, cfg: Any, **kwargs: Any) -> delivery_module.DeliveryResult:
        # The send itself succeeds, but a concurrent process claims the same
        # hash before our claim lands.
        store.sent.add(digest_key)
        return delivery_module.DeliveryResult(
            channel="telegram", status="sent", content_hash=content_hash(report)
        )

    monkeypatch.setattr(delivery_module, "send_telegram", _fake_send)
    results = deliver(_report(), telegram=_telegram_cfg(), store=store)

    assert results[0].status == "sent"
    assert results[0].reason == "duplicate_race"


class _CommitFailingStore:
    """Journal claim succeeds, but the surrounding transaction fails to COMMIT."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def was_sent(self, *args: Any) -> bool:
        return False

    def record_send(self, *args: Any) -> bool:
        self.records.append(args[-1])
        return True

    def atomic(self) -> Any:
        import contextlib

        @contextlib.contextmanager
        def guard() -> Any:
            yield
            raise RuntimeError("database is locked")

        return guard()


def test_commit_failure_after_send_is_reported_sent_not_failed(monkeypatch: Any) -> None:
    from lv24_seo.report import delivery

    monkeypatch.setattr(
        delivery,
        "send_telegram",
        lambda report, cfg: DeliveryResult(channel="telegram", status="sent", content_hash="h"),
    )
    store = _CommitFailingStore()
    results = deliver(_report(), telegram=TelegramConfig(bot_token="t", chat_id="1"), store=store)
    assert [(r.status, r.reason) for r in results] == [("sent", "delivery_journal_failed")]


def test_telegram_text_marks_truncation(monkeypatch: Any) -> None:
    from lv24_seo.report import delivery, render

    monkeypatch.setattr(render, "render_text", lambda report: "x" * 9000)
    text = delivery._telegram_text(_report())
    assert len(text) == delivery._TELEGRAM_LIMIT
    assert text.endswith(delivery._TELEGRAM_TRUNCATED)
    monkeypatch.setattr(render, "render_text", lambda report: "short")
    assert delivery._telegram_text(_report()) == "short"
