"""Report delivery: SMTP email and Telegram (Wave 5, WP5.3 + WP5.4).

Delivery is deliberately boring: build the message, hand it to an injectable transport, turn
*every* failure into a :class:`DeliveryResult` and never leak a password, token or SMTP host
into the reason. The optional store is the only memory of previous deliveries.

Idempotency rules
-----------------
``deliver`` consults an optional duck-typed store (see
:meth:`lv24_seo.store.repositories.Store.was_sent` / ``record_send``) with the tuple
``(kind, property_id, period, content_hash, channel)`` where ``period`` is
``"<period_start>..<period_end>"``. A report whose content hash was already delivered on a
channel is skipped instead of sent again. Only successful sends are recorded: a failed attempt
must stay retryable, so it never blocks the next run.

Rendering lives in :mod:`lv24_seo.report.render` and is imported lazily so that a partial
install fails with a typed :class:`SeoError` instead of an ``ImportError`` at import time.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from email.message import EmailMessage
from functools import partial
from typing import Any, Protocol

from lv24_seo.core.errors import INVALID_INPUT, UPSTREAM_UNAVAILABLE, SeoError
from lv24_seo.report.i18n import DEFAULT_LOCALE, normalize_locale, translator
from lv24_seo.report.models import Report

__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_TELEGRAM",
    "STATUS_FAILED",
    "STATUS_SENT",
    "STATUS_SKIPPED",
    "DeliveryResult",
    "EmailConfig",
    "TelegramConfig",
    "content_hash",
    "deliver",
    "send_email",
    "send_telegram",
]

CHANNEL_EMAIL = "email"
CHANNEL_TELEGRAM = "telegram"

STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

_REASON_ALREADY_SENT = "already_sent"
_REASON_MATERIAL = "no_material_change"

_TELEGRAM_LIMIT = 4000
_TELEGRAM_TRUNCATED = "\n... [truncated: see the email/HTML report for the full text]"
_TELEGRAM_TRUNCATED_KEYS = ("telegram.truncated", "email.truncated", "truncated")
_SUBJECT_FALLBACK = "SEO report"
_HEADER_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
_SMTP_TIMEOUT = 30.0
_HTTP_TIMEOUT = 15.0
_MISSING = object()

SmtpFactory = Callable[[str, int], Any]
HttpPost = Callable[[str, dict[str, Any]], Any]


class _Renderers(Protocol):
    """The slice of :mod:`lv24_seo.report.render` that delivery depends on."""

    def render_email_text(self, report: Report, *, locale: str = "en") -> str: ...

    def render_html(
        self, report: Report, *, title: str | None = None, locale: str = "en"
    ) -> str: ...

    def render_subject(self, report: Report, *, locale: str = "en") -> str: ...

    def render_markdown(self, report: Report) -> str: ...


@dataclass(frozen=True, slots=True)
class EmailConfig:
    """SMTP settings. ``use_tls`` means STARTTLS on a plain connection, not implicit TLS."""

    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    use_tls: bool = True
    recipients: tuple[str, ...] = ()
    locale: str = "en"


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    locale: str = "en"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Outcome of one channel attempt: ``sent``, ``skipped`` or ``failed``."""

    channel: str
    status: str
    reason: str = ""
    content_hash: str = ""


def content_hash(report: Report, locale: str = "en") -> str:
    """SHA-256 of the semantic report content used for delivery de-duplication.

    Volatile envelope fields (``generated_at``) and derived diagnostics
    (``limitations``, ``warnings``) are excluded: two builds of the same period
    with the same headline and sections share a hash even when rebuilt later.

    The normalized locale is part of the payload for every language except
    English. English deliberately omits the key so its digest stays identical to
    the pre-localization hash stored in existing ``sends`` ledgers: an English
    user upgrading the tool is not sent the same period twice. Switching to
    ``ru`` or ``zh`` produces a new digest and re-sends that period once.
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
    if normalized != DEFAULT_LOCALE:
        semantic["locale"] = normalized
    canonical = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def send_email(
    report: Report,
    cfg: EmailConfig,
    *,
    smtp_factory: SmtpFactory | None = None,
) -> DeliveryResult:
    """Send the report as a ``multipart/alternative`` message (plain text + HTML).

    ``smtp_factory`` is called as ``factory(host, port)`` and must return an object with the
    ``smtplib.SMTP`` surface (``starttls``, ``login``, ``sendmail``, ``quit``/``close``). It
    exists so tests and callers can supply their own transport without monkeypatching.

    The function never raises for configuration or transport problems: a missing sender or
    recipient list yields ``no_recipients``, and transport errors are reported by exception
    class name only, because SMTP exceptions may embed the host or credentials in their text.
    """
    digest = content_hash(report, cfg.locale)
    if not cfg.sender or not cfg.recipients:
        return DeliveryResult(
            channel=CHANNEL_EMAIL,
            status=STATUS_FAILED,
            reason="no_recipients",
            content_hash=digest,
        )

    smtp: Any | None = None
    try:
        message = _build_email(report, cfg)
        smtp = _open_smtp(cfg, smtp_factory)
        if cfg.use_tls:
            # A verifying context: without it smtplib silently accepts any
            # certificate, so credentials could leak to an active MITM.
            # login() below only runs after a verified handshake.
            smtp.starttls(context=ssl.create_default_context())
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        refused = smtp.sendmail(cfg.sender, list(cfg.recipients), message.as_string())
        if refused:
            return DeliveryResult(
                channel=CHANNEL_EMAIL,
                status=STATUS_FAILED,
                reason="recipients_refused",
                content_hash=digest,
            )
    except Exception as exc:
        return DeliveryResult(
            channel=CHANNEL_EMAIL,
            status=STATUS_FAILED,
            reason=type(exc).__name__,
            content_hash=digest,
        )
    finally:
        _close_smtp(smtp)

    return DeliveryResult(channel=CHANNEL_EMAIL, status=STATUS_SENT, content_hash=digest)


def send_telegram(
    report: Report,
    cfg: TelegramConfig,
    *,
    http_post: HttpPost | None = None,
) -> DeliveryResult:
    """Send a plain-text report to Telegram via the Bot API ``sendMessage`` method.

    ``http_post(url, json)`` is called positionally and may return ``None`` (fire-and-forget),
    an object with ``status_code``, or an object with ``raise_for_status()``. Missing token or
    chat id yields ``not_configured``; transport and HTTP failures yield ``failed``.
    """
    digest = content_hash(report, cfg.locale)
    if not cfg.bot_token or not cfg.chat_id:
        return DeliveryResult(
            channel=CHANNEL_TELEGRAM,
            status=STATUS_FAILED,
            reason="not_configured",
            content_hash=digest,
        )

    payload: dict[str, Any] = {
        "chat_id": cfg.chat_id,
        "text": _telegram_text(report, cfg.locale),
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    try:
        response = (http_post or _default_http_post)(url, payload)
    except Exception as exc:
        return DeliveryResult(
            channel=CHANNEL_TELEGRAM,
            status=STATUS_FAILED,
            reason=type(exc).__name__,
            content_hash=digest,
        )

    try:
        failure = _http_failure_reason(response)
    except Exception as exc:
        failure = type(exc).__name__
    if failure is not None:
        return DeliveryResult(
            channel=CHANNEL_TELEGRAM,
            status=STATUS_FAILED,
            reason=failure,
            content_hash=digest,
        )

    return DeliveryResult(channel=CHANNEL_TELEGRAM, status=STATUS_SENT, content_hash=digest)


def deliver(
    report: Report,
    *,
    email: EmailConfig | None = None,
    telegram: TelegramConfig | None = None,
    store: Any | None = None,
    only_on_change: bool = True,
    material_threshold: float = 0.0,
    material_thresholds: Mapping[str, float] | None = None,
    locale: str = "en",
) -> list[DeliveryResult]:
    """Deliver a report over every configured channel.

    ``store`` is duck-typed and may be any object exposing
    ``was_sent(kind, property_id, period, content_hash, channel) -> bool`` and
    ``record_send(kind, property_id, period, content_hash, channel) -> bool``; the shipping
    implementation is :class:`lv24_seo.store.repositories.Store`. Without a store every call
    sends, because there is nowhere to remember previous deliveries.

    Each channel renders and hashes with its own ``EmailConfig.locale`` /
    ``TelegramConfig.locale``; the ``locale`` argument is only a fallback for a
    channel config that carries no locale. This keeps ``was_sent`` /
    ``record_send`` aligned with the digest the sender actually produced.

    ``material_threshold`` (percent) or ``material_thresholds`` (per-metric mapping
    with keys ``clicks_pct`` / ``impressions_pct`` / ``position_abs``) suppresses
    delivery unless at least one section row carries a numeric change field at or
    above the gate, or a truthy ``significant`` flag set by the analysis layer.
    A row without any change field never opens the gate: silence means
    "no evidence of movement", not "movement of zero". Because the gate runs
    before the locale hash is consulted, ``only_on_change`` can still suppress a
    locale switch when the period has no material change.

    Retry policy: delivery is **at-least-once** per channel. A transport error
    after the remote side accepted the message is indistinguishable from "not
    sent", so an ambiguous failure is reported as ``failed`` (never recorded)
    and the next scheduled run may deliver a duplicate. Concurrent processes
    are de-duplicated best-effort via the store claim; when two processes race,
    the loser keeps ``sent`` with reason ``duplicate_race`` because its message
    already left. SMTP and the Telegram Bot API offer no exactly-once primitive
    to build on, so none is promised.
    """
    if material_threshold < 0:
        raise SeoError(
            INVALID_INPUT,
            "material_threshold must be zero or positive",
            next_action="Pass material_threshold=0 to disable the material-change gate.",
        )

    period = _period(report)

    channels: list[tuple[str, str, Callable[[], DeliveryResult]]] = []
    if email is not None:
        email_digest = content_hash(report, _channel_locale(email, locale))
        channels.append((CHANNEL_EMAIL, email_digest, partial(send_email, report, email)))
    if telegram is not None:
        telegram_digest = content_hash(report, _channel_locale(telegram, locale))
        channels.append(
            (CHANNEL_TELEGRAM, telegram_digest, partial(send_telegram, report, telegram))
        )

    if (
        only_on_change
        and (material_threshold > 0 or material_thresholds)
        and not _has_material_change(report, material_threshold, material_thresholds)
    ):
        return [
            DeliveryResult(
                channel=channel,
                status=STATUS_SKIPPED,
                reason=_REASON_MATERIAL,
                content_hash=channel_digest,
            )
            for channel, channel_digest, _ in channels
        ]

    results: list[DeliveryResult] = []
    for channel, channel_digest, send in channels:
        atomic = getattr(store, "atomic", None)
        guard = atomic() if callable(atomic) else contextlib.nullcontext()
        result: DeliveryResult | None = None
        try:
            with guard:
                result = _deliver_channel(
                    report, store, only_on_change, period, channel_digest, channel, send
                )
        except Exception:
            # A journal COMMIT can fail after the message already left: that is
            # still "sent" (with an unrecorded journal), never "failed".
            sent = result is not None and result.status == STATUS_SENT
            result = DeliveryResult(
                channel=channel,
                status=STATUS_SENT if sent else STATUS_FAILED,
                reason="delivery_journal_failed",
                content_hash=channel_digest,
            )
        results.append(result)
    return results


def _channel_locale(config: object, fallback: str) -> str:
    """Locale for one channel: its config wins, ``deliver(locale=...)`` is the fallback.

    A config that defines ``locale`` (even as an empty string) is authoritative,
    because :func:`send_email` / :func:`send_telegram` normalize that same value.
    The fallback only applies to duck-typed configs that predate the field.
    """
    configured = getattr(config, "locale", _MISSING)
    if configured is _MISSING:
        return normalize_locale(fallback)
    return normalize_locale(configured if isinstance(configured, str) else None)


def _deliver_channel(
    report: Report,
    store: Any,
    only_on_change: bool,
    period: str,
    digest: str,
    channel: str,
    send: Callable[[], DeliveryResult],
) -> DeliveryResult:
    """Serialize check/send/journal with the real store's cross-process write lock."""
    if (
        store is not None
        and only_on_change
        and _already_sent(store, report, period, digest, channel)
    ):
        return DeliveryResult(
            channel=channel,
            status=STATUS_SKIPPED,
            reason=_REASON_ALREADY_SENT,
            content_hash=digest,
        )
    result = send()
    if result.content_hash != digest:
        # Keep the returned digest identical to the ledger key even when a
        # custom transport or an older sender computes its own.
        result = replace(result, content_hash=digest)
    if store is not None and result.status == STATUS_SENT:
        try:
            recorded = store.record_send(report.kind, report.property_id, period, digest, channel)
        except Exception:
            return DeliveryResult(
                channel=channel,
                status=STATUS_SENT,
                reason="delivery_journal_failed",
                content_hash=digest,
            )
        if not recorded:
            return DeliveryResult(
                channel=channel,
                status=STATUS_SENT,
                reason="duplicate_race",
                content_hash=digest,
            )
    return result


def _build_email(report: Report, cfg: EmailConfig) -> EmailMessage:
    render = _renderers()
    locale = normalize_locale(cfg.locale)
    message = EmailMessage()
    message["Subject"] = _header_value(render.render_subject(report, locale=locale))
    message["From"] = cfg.sender
    message["To"] = ", ".join(cfg.recipients)
    message.set_content(render.render_email_text(report, locale=locale))
    message.add_alternative(render.render_html(report, locale=locale), subtype="html")
    # set_content/add_alternative rebuild content headers; set this last so it survives.
    message["Content-Language"] = locale
    return message


def _header_value(value: object) -> str:
    """Collapse ASCII whitespace: a newline in a header would make EmailMessage raise.

    Non-breaking spaces (used by the RU number format) are preserved.
    """
    header = _HEADER_WHITESPACE.sub(" ", str(value or "")).strip()
    return header or _SUBJECT_FALLBACK


def _telegram_text(report: Report, locale: str = "en") -> str:
    normalized = normalize_locale(locale)
    text = _renderers().render_email_text(report, locale=normalized)
    if len(text) <= _TELEGRAM_LIMIT:
        return text
    # Say so when the message is cut: a silently shortened report reads as complete.
    marker = _truncation_marker(normalized)
    return text[: _TELEGRAM_LIMIT - len(marker)] + marker


def _truncation_marker(locale: str) -> str:
    """Localized truncation note, falling back to English when no catalog key is present."""
    translate = translator(locale)
    for key in _TELEGRAM_TRUNCATED_KEYS:
        localized = translate.t(key)
        if localized and localized != key:
            return f"\n{localized}"
    return _TELEGRAM_TRUNCATED


def _renderers() -> _Renderers:
    """Import the renderer module lazily, with a typed error for partial installs."""
    try:
        from lv24_seo.report import render
    except ImportError as exc:
        raise SeoError(
            UPSTREAM_UNAVAILABLE,
            "report renderers are unavailable",
            next_action="Reinstall lv24-seo so lv24_seo.report.render is present.",
            retryable=False,
        ) from exc
    return render


def _open_smtp(cfg: EmailConfig, factory: SmtpFactory | None) -> Any:
    if factory is not None:
        return factory(cfg.host, cfg.port)
    return smtplib.SMTP(cfg.host, cfg.port, timeout=_SMTP_TIMEOUT)


def _close_smtp(smtp: Any | None) -> None:
    if smtp is None:
        return
    for method in ("quit", "close"):
        action = getattr(smtp, method, None)
        if callable(action):
            with contextlib.suppress(Exception):
                action()
            return


def _http_failure_reason(response: Any) -> str | None:
    """Map an injected HTTP response to a failure reason, or ``None`` when it succeeded.

    The default transport returns a bare ``int`` status code, while injected
    test doubles may return ``None``, a ``status_code`` object, a
    ``raise_for_status`` object or a decoded Bot API ``dict`` — all are
    normalized here so an HTTP 400/429 can never be reported as ``sent``.
    """
    if response is None:
        return None
    if isinstance(response, bool):
        return None if response else "http_unknown"
    if isinstance(response, int):
        if 200 <= response < 300:
            return None
        return f"http_{response}"
    if isinstance(response, Mapping):
        ok = response.get("ok")
        if ok is False:
            code = response.get("error_code")
            if isinstance(code, int):
                return f"http_{code}"
            return "telegram_error"
        if ok is True:
            return None
        # A mapping without an ``ok`` flag is not a success receipt.
        return "unexpected_response_shape"
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if 200 <= status < 300:
            return None
        return f"http_{status}"
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        try:
            raise_for_status()
        except Exception as exc:
            return type(exc).__name__
        return None
    return "unexpected_response_shape"


def _default_http_post(url: str, payload: dict[str, Any]) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # The scheme is a fixed HTTPS Bot API URL, never caller input.
    request = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=_HTTP_TIMEOUT
        ) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # The Bot API uses HTTP status codes for refusals (bad chat id, blocked bot, ...).
        return int(exc.code)


def _already_sent(
    store: Any,
    report: Report,
    period: str,
    digest: str,
    channel: str,
) -> bool:
    return bool(store.was_sent(report.kind, report.property_id, period, digest, channel))


_CHANGE_GATE_FIELDS: dict[str, tuple[str, ...]] = {
    "clicks_pct": ("clicks_change_pct", "clicks_delta_pct", "clicks_growth_pct"),
    "impressions_pct": (
        "impressions_change_pct",
        "impressions_delta_pct",
        "impressions_growth_pct",
    ),
    "position_abs": ("position_change", "position_delta"),
}
_GENERIC_CHANGE_MARKERS = ("change", "delta", "growth", "pct")


def _has_material_change(
    report: Report,
    threshold: float = 0.0,
    thresholds: Mapping[str, float] | None = None,
) -> bool:
    """Return True when at least one row shows movement at or above the gate."""
    gates = dict(thresholds) if thresholds else {}
    for section in report.to_dict().get("sections", []):
        rows = section.get("rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("significant"):
                return True
            for gate_key, fields in _CHANGE_GATE_FIELDS.items():
                gate = gates.get(gate_key, threshold)
                for field_name in fields:
                    value = row.get(field_name)
                    if isinstance(value, bool):
                        continue
                    if isinstance(value, (int, float)) and abs(value) >= gate:
                        return True
            if not gates and threshold > 0:
                # Legacy uniform gate: any change-like numeric field counts.
                for key, value in row.items():
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    lowered = str(key).lower()
                    change_like = "change" in lowered or any(
                        marker in lowered for marker in ("delta", "growth", "pct")
                    )
                    if change_like and abs(value) >= threshold:
                        return True
    return False


def _period(report: Report) -> str:
    return f"{report.period_start}..{report.period_end}"
