# ruff: noqa: RUF001
"""Localization catalogs and formatters for report presentation.

The catalogs intentionally mix Cyrillic, Latin and Han characters, so the
``RUF001`` ambiguous-unicode lint rule is disabled for this file.

Public API (see ``docs/plans/2026-11-12-email-templates.md``):

- :data:`SUPPORTED_LOCALES` / :data:`DEFAULT_LOCALE`
- :func:`normalize_locale` / :func:`translator`
- :class:`Translator`

Presentation only: canonical report JSON, snapshots, diffs and MCP payloads stay
English. Templates use single-pass ``{name}`` substitution; parameters are
inserted literally and stripped of C0, DEL and bidirectional control characters.
Missing keys fall back to English, then to the key itself; missing placeholders
render as an empty string. Numeric and date formatting never raise and never
invent values, and no system locale or ``strftime`` is used.

Placeholder contract for renderers:

- ``headline.significant.*``, ``issue.count.*`` and ``issue.checks.*`` are
  plural forms and must be rendered with :meth:`Translator.count`;
- ``subject.critical`` takes ``{count}``; the other ``subject.*`` keys take
  ``{period}`` or ``{change}``;
- ``notes.other_limitation`` takes ``{code}``;
- ``footer.property_id`` takes ``{property_id}``.

Plural keys carry all four CLDR forms (``one``/``few``/``many``/``other``) in
every catalog so catalog parity stays testable; English and Chinese repeat the
form they use for the unused categories.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "Translator",
    "normalize_locale",
    "translator",
]

SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru", "zh")
DEFAULT_LOCALE = "en"

_HTML_LANG: dict[str, str] = {"en": "en", "ru": "ru", "zh": "zh-Hans"}

# The Translator.date method shadows the imported ``date`` inside the class
# body, so class-level annotations use this module-level alias.
_Date = date

# locale -> (thousands separator, decimal separator)
_NUMBER_FORMATS: dict[str, tuple[str, str]] = {
    "en": (",", "."),
    "ru": ("\u00a0", ","),
    "zh": (",", "."),
}

_EN_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_RU_MONTHS: tuple[str, ...] = (
    "янв.",
    "февр.",
    "мар.",
    "апр.",
    "мая",
    "июня",
    "июля",
    "авг.",
    "сент.",
    "окт.",
    "нояб.",
    "дек.",
)

_EN_CATALOG: dict[str, str] = {
    # Brand and report framing
    "brand.name": "lv24-seo",
    "brand.suffix": "SEO report",
    "report.type.weekly": "Weekly report",
    "report.type.indexing": "Indexing report",
    "report.type.opportunities": "Opportunities report",
    "report.type.audit": "Site audit",
    "report.title": "SEO performance report",
    "report.preheader": "Search performance for the selected period.",
    # Labels
    "label.property": "Property",
    "label.period": "Period",
    "label.generated": "Generated",
    "label.vs_previous": "vs previous period",
    # KPI
    "kpi.clicks": "Clicks",
    "kpi.impressions": "Impressions",
    "kpi.ctr": "CTR",
    "kpi.position": "Avg. position",
    "kpi.previous": "Previous",
    "kpi.unknown": "Unknown",
    "kpi.no_data": "No data",
    "kpi.no_comparison": "No comparison",
    "kpi.significant": "Significant",
    "kpi.better": "Better",
    "kpi.worse": "Worse",
    "kpi.flat": "Flat",
    # Headline
    "headline.significant.one": "{count} metric changed significantly",
    "headline.significant.few": "{count} metrics changed significantly",
    "headline.significant.many": "{count} metrics changed significantly",
    "headline.significant.other": "{count} metrics changed significantly",
    "headline.improving": "Performance is improving.",
    "headline.worsening": "Performance is declining.",
    "headline.no_change": "Performance is stable.",
    "headline.no_comparison": "No previous period to compare.",
    "headline.no_data": "No search data for this period.",
    "headline.partial": "Some sources did not return data.",
    # Sections
    "section.opportunities": "Opportunities",
    "section.issues": "Issues",
    "section.sources": "Data sources",
    "section.notes": "Data notes",
    "section.warnings": "Warnings",
    "section.digest": "Attributed change digest",
    "section.cwv": "Core Web Vitals by template",
    # Traffic
    "traffic.rows": "Landing pages",
    "traffic.sessions": "Sessions",
    "traffic.no_rows": "No sessions in this period.",
    # Engines
    "engine.gsc": "Google Search Console",
    "engine.yandex_webmaster": "Yandex Webmaster",
    "engine.yandex_metrika": "Yandex Metrica",
    "engine.bing": "Bing Webmaster Tools",
    "engine.crux": "Chrome UX Report",
    "engine.psi": "PageSpeed Insights",
    # Sources
    "source.rows": "Rows",
    "source.missing": "No data",
    "source.kind.field": "Field data",
    "source.kind.lab": "Lab data",
    "source.kind.traffic": "Traffic data",
    "source.top_rows": "top query rows",
    # Opportunities
    "opportunity.kind.striking_distance": "Striking distance",
    "opportunity.kind.ctr_gap": "CTR gap",
    "opportunity.kind.decay": "Declining page",
    "opportunity.query": "Query",
    "opportunity.page": "Page",
    "opportunity.page_missing": "Page not set",
    "opportunity.position": "Position",
    "opportunity.impressions": "Impressions",
    "opportunity.expected_clicks": "Expected clicks",
    "opportunity.estimate": "Estimate",
    "opportunity.none": "No opportunities found for this period.",
    "opportunity.no_data": "No query data to evaluate opportunities.",
    "opportunity.top3": "Top 3 by expected clicks",
    "opportunity.kind.unknown": "Other opportunity",
    # Issues
    "issue.count.one": "{count} issue",
    "issue.count.few": "{count} issues",
    "issue.count.many": "{count} issues",
    "issue.count.other": "{count} issues",
    "issue.checks.one": "{count} rule",
    "issue.checks.few": "{count} rules",
    "issue.checks.many": "{count} rules",
    "issue.checks.other": "{count} rules",
    "issue.critical": "Critical",
    "issue.high": "High",
    "issue.medium": "Medium",
    "issue.low": "Low",
    "issue.unknown": "Unknown",
    "issue.no_data": "No issue data was provided.",
    "issue.zero": "No issues found.",
    # Notes and warnings
    "notes.original": "Provider text is shown in its original language.",
    "notes.estimates": ("Expected clicks are a prioritisation estimate, not a traffic promise."),
    "notes.other_limitation": "Other limitation ({code})",
    "notes.source_totals": (
        "Source metrics are sums of the fetched top rows, not full-window totals."
    ),
    "notes.limitation.top_3000_queries": "Yandex covers only the top 3,000 queries.",
    "notes.limitation.no_query_page_join": "Yandex does not join queries to pages.",
    "notes.limitation.sessions_not_clicks": "Yandex Metrica counts sessions, not clicks.",
    "notes.limitation.rows_cap_50000": "The provider caps responses at 50,000 rows.",
    "notes.limitation.sampled": "The provider returned sampled data.",
    "notes.limitation.low_traffic": "Traffic is too low for reliable field data.",
    "notes.limitation.degraded_host": "The provider reported a degraded host.",
    "notes.limitation.one_url_per_call": "The provider accepts one URL per call.",
    "notes.limitation.no_per_url_sitemap_errors": (
        "The provider does not report sitemap errors per URL."
    ),
    "notes.limitation.token_expiring": "The provider token expires soon.",
    "notes.limitation.paid_provider": "This provider requires a paid plan.",
    "notes.limitation.partial_response": "The provider returned a partial response.",
    "notes.limitation.empty_history": "The provider has no history for this window yet.",
    "notes.limitation.digest_no_comparison": (
        "Change digest: no comparison window, so metric changes cannot be attributed."
    ),
    "notes.limitation.digest_no_evidence": (
        "Change digest: no crawl, indexing or page-state evidence was supplied."
    ),
    "notes.limitation.digest_no_matching_evidence": (
        "Change digest: stored evidence did not match every material change."
    ),
    "notes.limitation.digest_evidence_malformed": (
        "Change digest: some evidence rows were ignored because they lacked an identifier."
    ),
    "notes.limitation.cwv_no_rows": "Core Web Vitals: no stored metric rows were provided.",
    "notes.limitation.cwv_template_missing": (
        "Core Web Vitals: rows without a URL template (including origin-level data) were skipped."
    ),
    "notes.limitation.cwv_device_missing": ("Core Web Vitals: rows without a device were skipped."),
    "notes.limitation.cwv_no_comparison": (
        "Core Web Vitals: no previous p75 baseline, so only current values are shown."
    ),
    "notes.limitation.cwv_partial_comparison": (
        "Core Web Vitals: only some URLs had a previous p75, so the comparison covers "
        "part of the sample."
    ),
    "notes.limitation.cwv_p75_worst_page": (
        "Core Web Vitals: a template p75 is the worst page p75 in that template; "
        "percentiles are not averaged across URLs."
    ),
    "notes.limitation.cwv_values_ignored": (
        "Core Web Vitals: some rows were ignored because their metric or p75 value was invalid."
    ),
    "notes.limitation.cwv_lab_ignored": (
        "Core Web Vitals: lab (Lighthouse) rows were ignored; only field p75 data is grouped."
    ),
    # Attributed change digest
    "digest.count.one": "{count} material change",
    "digest.count.few": "{count} material changes",
    "digest.count.many": "{count} material changes",
    "digest.count.other": "{count} material changes",
    "digest.cause": "Candidate cause",
    "digest.confidence": "Confidence",
    "digest.evidence": "Evidence",
    "digest.next_action": "Next action",
    "digest.cause.new_crawl_issue": "New crawl issue",
    "digest.cause.resolved_crawl_issue": "Resolved crawl issue",
    "digest.cause.indexing_event": "Indexing event",
    "digest.cause.page_state": "Page state",
    "digest.cause.unexplained": "No matching evidence",
    "digest.cause.unknown": "Unclassified cause",
    "digest.confidence.high": "High",
    "digest.confidence.medium": "Medium",
    "digest.confidence.low": "Low",
    "digest.field.status": "Status",
    "digest.field.index_state": "Index state",
    "digest.field.canonical": "Canonical",
    "digest.none": "No material metric change to explain.",
    "digest.no_comparison": "No comparison window: metric changes cannot be attributed.",
    "digest.no_evidence": (
        "No crawl issue, indexing event or page state evidence was provided; "
        "causes are not attributed."
    ),
    "digest.truncated": "Showing {shown} of {total} changes.",
    "digest.data_age": "Oldest evidence: {date}",
    "digest.unknown_target": "the reported issue",
    "digest.next_action.new_crawl_issue": ("Fix {target}, then re-crawl and re-check {metric}."),
    "digest.next_action.resolved_crawl_issue": (
        "Confirm the {target} fix is live, then re-check {metric} after the next crawl."
    ),
    "digest.next_action.indexing_event": (
        "Review the indexing event for {target}, then request a recrawl."
    ),
    "digest.next_action.page_state": (
        "Restore {target}, then re-check {metric} after the next crawl."
    ),
    "digest.next_action.unexplained": (
        "Collect crawl issues, indexing events and page state for the affected URLs; "
        "no cause is attributed."
    ),
    # Core Web Vitals by template
    "cwv.template": "Template",
    "cwv.device": "Device",
    "cwv.p75": "p75",
    "cwv.sample": "Sample",
    "cwv.coverage": "Coverage",
    "cwv.regressed": "Regressed",
    "cwv.no_regression": "No template regressed against the previous window.",
    "cwv.no_comparison": "No comparison window: only current p75 values are shown.",
    "cwv.none": "No stored Core Web Vitals rows.",
    "cwv.no_templates": (
        "Stored CWV rows cannot be grouped: a URL template and device are required."
    ),
    "cwv.truncated": "Showing {shown} of {total} templates.",
    "cwv.data_age": "Oldest CWV data: {date}",
    "cwv.regressed_summary": "Regressed: {template} · {device} ({metric} {change})",
    "cwv.count.one": "{count} template",
    "cwv.count.few": "{count} templates",
    "cwv.count.many": "{count} templates",
    "cwv.count.other": "{count} templates",
    "cwv.device.phone": "Phone",
    "cwv.device.desktop": "Desktop",
    "cwv.device.tablet": "Tablet",
    "cwv.status.good": "Good",
    "cwv.status.needs_improvement": "Needs improvement",
    "cwv.status.poor": "Poor",
    "cwv.status.unknown": "Unknown",
    "cwv.metric.lcp": "LCP",
    "cwv.metric.inp": "INP",
    "cwv.metric.cls": "CLS",
    "cwv.metric.fcp": "FCP",
    "cwv.metric.ttfb": "TTFB",
    "cwv.metric.fid": "FID",
    "warning.title": "Warnings",
    "warning.no_metrics": "No metrics for either period; delta rows are all zero.",
    "warning.previous_no_metrics": (
        "The previous period has no metrics, so deltas cannot be significant."
    ),
    "warning.no_provider_rows": ("No provider returned rows; traffic and opportunities are empty."),
    "generic.no_sections": "No sections in this report.",
    "generic.other_metrics": "Other metrics",
    "generic.field": "Field",
    "generic.value": "Value",
    "telegram.truncated": "... [truncated: see the email/HTML report for the full text]",
    # Footer
    "footer.generated_by": "Generated by lv24-seo",
    "footer.property_id": "Property ID: {property_id}",
    "footer.license": "Apache-2.0 licensed",
    # Subjects
    "subject.weekly": "Weekly SEO report: {period}",
    "subject.no_data": "No search data for {period}",
    "subject.no_comparison": "SEO report without comparison: {period}",
    "subject.critical": "Critical SEO issues: {count}",
    "subject.clicks": "Clicks: {change}",
    "subject.impressions": "Impressions: {change}",
    "subject.ctr": "CTR: {change}",
    "subject.position": "Avg. position: {change}",
    # Units
    "unit.clicks": "clicks",
    "unit.impressions": "impressions",
    "unit.rows": "rows",
    "unit.sessions": "sessions",
    "unit.positions": "positions",
    "unit.ms": "ms",
    "unit.pp": "pp",
}

_RU_CATALOG: dict[str, str] = {
    # Brand and report framing
    "brand.name": "lv24-seo",
    "brand.suffix": "SEO-отчёт",
    "report.type.weekly": "Недельный отчёт",
    "report.type.indexing": "Отчёт по индексации",
    "report.type.opportunities": "Отчёт по возможностям",
    "report.type.audit": "Аудит сайта",
    "report.title": "Отчёт по SEO-показателям",
    "report.preheader": "Поисковые показатели за выбранный период.",
    # Labels
    "label.property": "Ресурс",
    "label.period": "Период",
    "label.generated": "Сформирован",
    "label.vs_previous": "к предыдущему периоду",
    # KPI
    "kpi.clicks": "Клики",
    "kpi.impressions": "Показы",
    "kpi.ctr": "CTR",
    "kpi.position": "Ср. позиция",
    "kpi.previous": "Ранее",
    "kpi.unknown": "Неизвестно",
    "kpi.no_data": "Нет данных",
    "kpi.no_comparison": "Нет сравнения",
    "kpi.significant": "Значимо",
    "kpi.better": "Лучше",
    "kpi.worse": "Хуже",
    "kpi.flat": "Без изменений",
    # Headline
    "headline.significant.one": "{count} показатель значительно изменился",
    "headline.significant.few": "{count} показателя значительно изменились",
    "headline.significant.many": "{count} показателей значительно изменилось",
    "headline.significant.other": "{count} показателей значительно изменилось",
    "headline.improving": "Показатели улучшаются.",
    "headline.worsening": "Показатели ухудшаются.",
    "headline.no_change": "Показатели стабильны.",
    "headline.no_comparison": "Нет предыдущего периода для сравнения.",
    "headline.no_data": "За этот период нет данных о поиске.",
    "headline.partial": "Часть источников не вернула данные.",
    # Sections
    "section.opportunities": "Возможности",
    "section.issues": "Проблемы",
    "section.sources": "Источники данных",
    "section.notes": "Примечания к данным",
    "section.warnings": "Предупреждения",
    "section.digest": "Разбор изменений",
    "section.cwv": "Core Web Vitals по шаблонам",
    # Traffic
    "traffic.rows": "Страницы входа",
    "traffic.sessions": "Сеансы",
    "traffic.no_rows": "За этот период нет сеансов.",
    # Engines
    "engine.gsc": "Google Search Console",
    "engine.yandex_webmaster": "Яндекс Вебмастер",
    "engine.yandex_metrika": "Яндекс Метрика",
    "engine.bing": "Bing Webmaster Tools",
    "engine.crux": "Chrome UX Report",
    "engine.psi": "PageSpeed Insights",
    # Sources
    "source.rows": "Строки",
    "source.missing": "Нет данных",
    "source.kind.field": "Полевые данные",
    "source.kind.lab": "Лабораторные данные",
    "source.kind.traffic": "Данные трафика",
    "source.top_rows": "верхние строки запросов",
    # Opportunities
    "opportunity.kind.striking_distance": "Близко к топ-10",
    "opportunity.kind.ctr_gap": "Низкий CTR",
    "opportunity.kind.decay": "Снижение трафика",
    "opportunity.query": "Запрос",
    "opportunity.page": "Страница",
    "opportunity.page_missing": "Страница не указана",
    "opportunity.position": "Позиция",
    "opportunity.impressions": "Показы",
    "opportunity.expected_clicks": "Ожидаемые клики",
    "opportunity.estimate": "Оценка",
    "opportunity.none": "За этот период возможностей не найдено.",
    "opportunity.no_data": "Нет данных по запросам для оценки возможностей.",
    "opportunity.top3": "Топ-3 по ожидаемым кликам",
    "opportunity.kind.unknown": "Другая возможность",
    # Issues
    "issue.count.one": "{count} проблема",
    "issue.count.few": "{count} проблемы",
    "issue.count.many": "{count} проблем",
    "issue.count.other": "{count} проблем",
    "issue.checks.one": "{count} правило",
    "issue.checks.few": "{count} правила",
    "issue.checks.many": "{count} правил",
    "issue.checks.other": "{count} правил",
    "issue.critical": "Критические",
    "issue.high": "Высокие",
    "issue.medium": "Средние",
    "issue.low": "Низкие",
    "issue.unknown": "Неизвестно",
    "issue.no_data": "Данные о проблемах не предоставлены.",
    "issue.zero": "Проблем не найдено.",
    # Notes and warnings
    "notes.original": "Тексты провайдеров приведены на языке оригинала.",
    "notes.estimates": "Ожидаемые клики — оценка приоритета, а не гарантия трафика.",
    "notes.other_limitation": "Другое ограничение ({code})",
    "notes.source_totals": (
        "Показатели источников — суммы по загруженным верхним строкам, а не итоги за весь период."
    ),
    "notes.limitation.top_3000_queries": "Яндекс покрывает только топ-3 000 запросов.",
    "notes.limitation.no_query_page_join": "Яндекс не связывает запросы со страницами.",
    "notes.limitation.sessions_not_clicks": "Яндекс Метрика считает сеансы, а не клики.",
    "notes.limitation.rows_cap_50000": "Провайдер ограничивает ответ 50 000 строками.",
    "notes.limitation.sampled": "Провайдер вернул выборочные данные.",
    "notes.limitation.low_traffic": "Трафика слишком мало для надёжных полевых данных.",
    "notes.limitation.degraded_host": "Провайдер сообщил о деградации хоста.",
    "notes.limitation.one_url_per_call": "Провайдер принимает один URL за запрос.",
    "notes.limitation.no_per_url_sitemap_errors": (
        "Провайдер не сообщает ошибки карты сайта по каждому URL."
    ),
    "notes.limitation.token_expiring": "Срок действия токена провайдера скоро истекает.",
    "notes.limitation.paid_provider": "Этот провайдер требует платного тарифа.",
    "notes.limitation.partial_response": "Провайдер вернул частичный ответ.",
    "notes.limitation.empty_history": "У провайдера пока нет истории за это окно.",
    "notes.limitation.digest_no_comparison": (
        "Разбор изменений: нет окна сравнения, поэтому изменения показателей нельзя объяснить."
    ),
    "notes.limitation.digest_no_evidence": (
        "Разбор изменений: не переданы данные о проблемах краула, индексации и состоянии страниц."
    ),
    "notes.limitation.digest_no_matching_evidence": (
        "Разбор изменений: сохранённые доказательства подошли не ко всем значимым изменениям."
    ),
    "notes.limitation.digest_evidence_malformed": (
        "Разбор изменений: часть строк доказательств пропущена из-за отсутствия идентификатора."
    ),
    "notes.limitation.cwv_no_rows": "Core Web Vitals: сохранённые строки метрик не переданы.",
    "notes.limitation.cwv_template_missing": (
        "Core Web Vitals: строки без шаблона URL (включая данные уровня origin) пропущены."
    ),
    "notes.limitation.cwv_device_missing": "Core Web Vitals: строки без устройства пропущены.",
    "notes.limitation.cwv_no_comparison": (
        "Core Web Vitals: нет предыдущего p75 для сравнения, показаны только текущие значения."
    ),
    "notes.limitation.cwv_partial_comparison": (
        "Core Web Vitals: предыдущий p75 был не у всех URL, поэтому сравнение "
        "покрывает часть выборки."
    ),
    "notes.limitation.cwv_p75_worst_page": (
        "Core Web Vitals: p75 шаблона — это худший p75 страницы в шаблоне; "
        "перцентили не усредняются по URL."
    ),
    "notes.limitation.cwv_values_ignored": (
        "Core Web Vitals: часть строк пропущена из-за недопустимой метрики или значения p75."
    ),
    "notes.limitation.cwv_lab_ignored": (
        "Core Web Vitals: лабораторные строки (Lighthouse) пропущены; "
        "группируются только полевые p75."
    ),
    # Attributed change digest
    "digest.count.one": "{count} значимое изменение",
    "digest.count.few": "{count} значимых изменения",
    "digest.count.many": "{count} значимых изменений",
    "digest.count.other": "{count} значимых изменений",
    "digest.cause": "Возможная причина",
    "digest.confidence": "Уверенность",
    "digest.evidence": "Доказательства",
    "digest.next_action": "Следующее действие",
    "digest.cause.new_crawl_issue": "Новая проблема краула",
    "digest.cause.resolved_crawl_issue": "Устранённая проблема краула",
    "digest.cause.indexing_event": "Событие индексации",
    "digest.cause.page_state": "Состояние страницы",
    "digest.cause.unexplained": "Подходящих доказательств нет",
    "digest.cause.unknown": "Причина не определена",
    "digest.confidence.high": "Высокая",
    "digest.confidence.medium": "Средняя",
    "digest.confidence.low": "Низкая",
    "digest.field.status": "Статус",
    "digest.field.index_state": "Состояние индексации",
    "digest.field.canonical": "Canonical",
    "digest.none": "Нет значимых изменений показателей для объяснения.",
    "digest.no_comparison": "Нет окна сравнения: изменения показателей нельзя объяснить.",
    "digest.no_evidence": (
        "Нет данных о проблемах краула, событиях индексации и состоянии страниц; "
        "причины не указываются."
    ),
    "digest.truncated": "Показано {shown} из {total} изменений.",
    "digest.data_age": "Самое старое доказательство: {date}",
    "digest.unknown_target": "указанная проблема",
    "digest.next_action.new_crawl_issue": (
        "Исправьте {target}, затем пересканируйте сайт и проверьте {metric}."
    ),
    "digest.next_action.resolved_crawl_issue": (
        "Убедитесь, что исправление {target} развёрнуто, и проверьте {metric} "
        "после следующего краула."
    ),
    "digest.next_action.indexing_event": (
        "Проверьте событие индексации для {target} и запросите переобход."
    ),
    "digest.next_action.page_state": (
        "Восстановите {target} и проверьте {metric} после следующего краула."
    ),
    "digest.next_action.unexplained": (
        "Соберите проблемы краула, события индексации и состояние страниц для затронутых "
        "URL; причина не указывается."
    ),
    # Core Web Vitals by template
    "cwv.template": "Шаблон",
    "cwv.device": "Устройство",
    "cwv.p75": "p75",
    "cwv.sample": "Выборка",
    "cwv.coverage": "Покрытие",
    "cwv.regressed": "Ухудшение",
    "cwv.no_regression": "Ни один шаблон не ухудшился относительно прошлого окна.",
    "cwv.no_comparison": "Нет окна сравнения: показаны только текущие значения p75.",
    "cwv.none": "Сохранённых строк Core Web Vitals нет.",
    "cwv.no_templates": (
        "Сохранённые строки CWV нельзя сгруппировать: нужны шаблон URL и устройство."
    ),
    "cwv.truncated": "Показано {shown} из {total} шаблонов.",
    "cwv.data_age": "Самое старое измерение CWV: {date}",
    "cwv.regressed_summary": "Ухудшение: {template} · {device} ({metric} {change})",
    "cwv.count.one": "{count} шаблон",
    "cwv.count.few": "{count} шаблона",
    "cwv.count.many": "{count} шаблонов",
    "cwv.count.other": "{count} шаблонов",
    "cwv.device.phone": "Телефон",
    "cwv.device.desktop": "Компьютер",
    "cwv.device.tablet": "Планшет",
    "cwv.status.good": "Хорошо",
    "cwv.status.needs_improvement": "Требует улучшения",
    "cwv.status.poor": "Плохо",
    "cwv.status.unknown": "Неизвестно",
    "cwv.metric.lcp": "LCP",
    "cwv.metric.inp": "INP",
    "cwv.metric.cls": "CLS",
    "cwv.metric.fcp": "FCP",
    "cwv.metric.ttfb": "TTFB",
    "cwv.metric.fid": "FID",
    "warning.title": "Предупреждения",
    "warning.no_metrics": "За оба периода нет метрик; строки изменений нулевые.",
    "warning.previous_no_metrics": (
        "За предыдущий период нет метрик, поэтому изменения не могут быть значимыми."
    ),
    "warning.no_provider_rows": ("Ни один провайдер не вернул строки; трафик и возможности пусты."),
    "generic.no_sections": "В отчёте нет разделов.",
    "generic.other_metrics": "Другие метрики",
    "generic.field": "Поле",
    "generic.value": "Значение",
    "telegram.truncated": "... [сокращено: полный текст — в письме/HTML-отчёте]",
    # Footer
    "footer.generated_by": "Сформировано сервисом lv24-seo",
    "footer.property_id": "ID ресурса: {property_id}",
    "footer.license": "Лицензия Apache-2.0",
    # Subjects
    "subject.weekly": "Недельный SEO-отчёт: {period}",
    "subject.no_data": "Нет данных о поиске: {period}",
    "subject.no_comparison": "SEO-отчёт без сравнения: {period}",
    "subject.critical": "Критические проблемы SEO: {count}",
    "subject.clicks": "Клики: {change}",
    "subject.impressions": "Показы: {change}",
    "subject.ctr": "CTR: {change}",
    "subject.position": "Ср. позиция: {change}",
    # Units
    "unit.clicks": "клики",
    "unit.impressions": "показы",
    "unit.rows": "строки",
    "unit.sessions": "сеансы",
    "unit.positions": "позиции",
    "unit.ms": "мс",
    "unit.pp": "п.п.",
}

_ZH_CATALOG: dict[str, str] = {
    # Brand and report framing
    "brand.name": "lv24-seo",
    "brand.suffix": "SEO 报告",
    "report.type.weekly": "每周报告",
    "report.type.indexing": "索引报告",
    "report.type.opportunities": "优化机会报告",
    "report.type.audit": "网站审计",
    "report.title": "SEO 表现报告",
    "report.preheader": "所选期间的搜索表现摘要。",
    # Labels
    "label.property": "资源",
    "label.period": "时间范围",
    "label.generated": "生成时间",
    "label.vs_previous": "较上一周期",
    # KPI
    "kpi.clicks": "点击次数",
    "kpi.impressions": "展示次数",
    "kpi.ctr": "点击率",
    "kpi.position": "平均排名",
    "kpi.previous": "上期",
    "kpi.unknown": "未知",
    "kpi.no_data": "无数据",
    "kpi.no_comparison": "无可比数据",
    "kpi.significant": "显著",
    "kpi.better": "改善",
    "kpi.worse": "变差",
    "kpi.flat": "持平",
    # Headline
    "headline.significant.one": "{count} 项指标变化显著",
    "headline.significant.few": "{count} 项指标变化显著",
    "headline.significant.many": "{count} 项指标变化显著",
    "headline.significant.other": "{count} 项指标变化显著",
    "headline.improving": "表现正在改善。",
    "headline.worsening": "表现正在下滑。",
    "headline.no_change": "表现保持稳定。",
    "headline.no_comparison": "缺少上一周期数据，无法比较。",
    "headline.no_data": "该周期没有搜索数据。",
    "headline.partial": "部分来源未返回数据。",
    # Sections
    "section.opportunities": "优化机会",
    "section.issues": "问题",
    "section.sources": "数据来源",
    "section.notes": "数据说明",
    "section.warnings": "警告",
    "section.digest": "变化归因摘要",
    "section.cwv": "按模板划分的 Core Web Vitals",
    # Traffic
    "traffic.rows": "着陆页",
    "traffic.sessions": "会话数",
    "traffic.no_rows": "该周期没有会话。",
    # Engines
    "engine.gsc": "Google Search Console",
    "engine.yandex_webmaster": "Yandex Webmaster",
    "engine.yandex_metrika": "Yandex Metrica",
    "engine.bing": "Bing 网站管理员工具",
    "engine.crux": "Chrome 用户体验报告",
    "engine.psi": "PageSpeed Insights",
    # Sources
    "source.rows": "行数",
    "source.missing": "无数据",
    "source.kind.field": "实测数据",
    "source.kind.lab": "实验室数据",
    "source.kind.traffic": "流量数据",
    "source.top_rows": "热门查询行",
    # Opportunities
    "opportunity.kind.striking_distance": "接近首页排名",
    "opportunity.kind.ctr_gap": "点击率差距",
    "opportunity.kind.decay": "流量下滑页面",
    "opportunity.query": "查询词",
    "opportunity.page": "页面",
    "opportunity.page_missing": "未指定页面",
    "opportunity.position": "排名",
    "opportunity.impressions": "展示次数",
    "opportunity.expected_clicks": "预计点击次数",
    "opportunity.estimate": "预估",
    "opportunity.none": "该周期未发现优化机会。",
    "opportunity.no_data": "没有查询数据可用于评估优化机会。",
    "opportunity.top3": "按预计点击次数排序的前 3 项",
    "opportunity.kind.unknown": "其他机会",
    # Issues
    "issue.count.one": "{count} 个问题",
    "issue.count.few": "{count} 个问题",
    "issue.count.many": "{count} 个问题",
    "issue.count.other": "{count} 个问题",
    "issue.checks.one": "{count} 条规则",
    "issue.checks.few": "{count} 条规则",
    "issue.checks.many": "{count} 条规则",
    "issue.checks.other": "{count} 条规则",
    "issue.critical": "严重",
    "issue.high": "高",
    "issue.medium": "中",
    "issue.low": "低",
    "issue.unknown": "未知",
    "issue.no_data": "未提供问题数据。",
    "issue.zero": "未发现问题。",
    # Notes and warnings
    "notes.original": "提供商原文按原语言显示。",
    "notes.estimates": "预计点击次数为优先级预估，并非流量承诺。",
    "notes.other_limitation": "其他限制（{code}）",
    "notes.source_totals": "来源指标为已获取热门行的汇总，而非整个周期的总量。",
    "notes.limitation.top_3000_queries": "Yandex 仅覆盖前 3,000 个查询。",
    "notes.limitation.no_query_page_join": "Yandex 不提供查询与页面的关联。",
    "notes.limitation.sessions_not_clicks": "Yandex Metrica 统计会话，而非点击。",
    "notes.limitation.rows_cap_50000": "提供商将响应限制为 50,000 行。",
    "notes.limitation.sampled": "提供商返回了抽样数据。",
    "notes.limitation.low_traffic": "流量过低，无法提供可靠的实测数据。",
    "notes.limitation.degraded_host": "提供商报告主机性能下降。",
    "notes.limitation.one_url_per_call": "该提供商每次调用仅支持一个网址。",
    "notes.limitation.no_per_url_sitemap_errors": "该提供商不按网址报告站点地图错误。",
    "notes.limitation.token_expiring": "提供商令牌即将过期。",
    "notes.limitation.paid_provider": "该提供商需要付费方案。",
    "notes.limitation.partial_response": "提供商返回了部分响应。",
    "notes.limitation.empty_history": "提供商暂无该时间窗口的历史数据。",
    "notes.limitation.digest_no_comparison": "变化归因：没有对比周期，无法归因指标变化。",
    "notes.limitation.digest_no_evidence": "变化归因：未提供抓取、索引或页面状态证据。",
    "notes.limitation.digest_no_matching_evidence": "变化归因：现有证据未能匹配所有显著变化。",
    "notes.limitation.digest_evidence_malformed": "变化归因：部分证据行因缺少标识而被忽略。",
    "notes.limitation.cwv_no_rows": "Core Web Vitals：未提供已存储的指标行。",
    "notes.limitation.cwv_template_missing": (
        "Core Web Vitals：缺少 URL 模板（包括来源级数据）的行已跳过。"
    ),
    "notes.limitation.cwv_device_missing": "Core Web Vitals：缺少设备的行已跳过。",
    "notes.limitation.cwv_no_comparison": (
        "Core Web Vitals：没有上一周期的 p75 基线，仅显示当前值。"
    ),
    "notes.limitation.cwv_partial_comparison": (
        "Core Web Vitals：只有部分网址有上一周期的 p75，因此对比仅覆盖部分样本。"
    ),
    "notes.limitation.cwv_p75_worst_page": (
        "Core Web Vitals：模板 p75 是该模板中最差页面的 p75；百分位数不跨网址平均。"
    ),
    "notes.limitation.cwv_values_ignored": ("Core Web Vitals：部分行因指标或 p75 值无效而被忽略。"),
    "notes.limitation.cwv_lab_ignored": (
        "Core Web Vitals：实验室（Lighthouse）行已忽略；仅分组实测 p75。"
    ),
    # Attributed change digest
    "digest.count.one": "{count} 项显著变化",
    "digest.count.few": "{count} 项显著变化",
    "digest.count.many": "{count} 项显著变化",
    "digest.count.other": "{count} 项显著变化",
    "digest.cause": "可能原因",
    "digest.confidence": "置信度",
    "digest.evidence": "证据",
    "digest.next_action": "下一步",
    "digest.cause.new_crawl_issue": "新的抓取问题",
    "digest.cause.resolved_crawl_issue": "已解决的抓取问题",
    "digest.cause.indexing_event": "索引事件",
    "digest.cause.page_state": "页面状态",
    "digest.cause.unexplained": "没有匹配的证据",
    "digest.cause.unknown": "未归类的原因",
    "digest.confidence.high": "高",
    "digest.confidence.medium": "中",
    "digest.confidence.low": "低",
    "digest.field.status": "状态码",
    "digest.field.index_state": "索引状态",
    "digest.field.canonical": "规范链接",
    "digest.none": "没有需要解释的显著指标变化。",
    "digest.no_comparison": "没有对比周期，无法归因指标变化。",
    "digest.no_evidence": "未提供抓取问题、索引事件或页面状态证据，因此不归因原因。",
    "digest.truncated": "显示 {shown}/{total} 项变化。",
    "digest.data_age": "最早证据：{date}",
    "digest.unknown_target": "所报告的问题",
    "digest.next_action.new_crawl_issue": "修复 {target}，然后重新抓取并复查 {metric}。",
    "digest.next_action.resolved_crawl_issue": (
        "确认 {target} 的修复已上线，然后在下次抓取后复查 {metric}。"
    ),
    "digest.next_action.indexing_event": "查看 {target} 的索引事件，然后申请重新抓取。",
    "digest.next_action.page_state": "恢复 {target}，然后在下次抓取后复查 {metric}。",
    "digest.next_action.unexplained": (
        "收集受影响网址的抓取问题、索引事件和页面状态；不归因原因。"
    ),
    # Core Web Vitals by template
    "cwv.template": "模板",
    "cwv.device": "设备",
    "cwv.p75": "p75",
    "cwv.sample": "样本",
    "cwv.coverage": "覆盖率",
    "cwv.regressed": "已退化",
    "cwv.no_regression": "与上一窗口相比，没有模板退化。",
    "cwv.no_comparison": "没有对比窗口：仅显示当前 p75 值。",
    "cwv.none": "没有已存储的 Core Web Vitals 行。",
    "cwv.no_templates": "已存储的 CWV 行无法分组：需要 URL 模板和设备。",
    "cwv.truncated": "显示 {shown}/{total} 个模板。",
    "cwv.data_age": "最早的 CWV 数据：{date}",
    "cwv.regressed_summary": "退化：{template} · {device}（{metric} {change}）",
    "cwv.count.one": "{count} 个模板",
    "cwv.count.few": "{count} 个模板",
    "cwv.count.many": "{count} 个模板",
    "cwv.count.other": "{count} 个模板",
    "cwv.device.phone": "手机",
    "cwv.device.desktop": "桌面",
    "cwv.device.tablet": "平板",
    "cwv.status.good": "良好",
    "cwv.status.needs_improvement": "需要改进",
    "cwv.status.poor": "较差",
    "cwv.status.unknown": "未知",
    "cwv.metric.lcp": "LCP",
    "cwv.metric.inp": "INP",
    "cwv.metric.cls": "CLS",
    "cwv.metric.fcp": "FCP",
    "cwv.metric.ttfb": "TTFB",
    "cwv.metric.fid": "FID",
    "warning.title": "警告",
    "warning.no_metrics": "两个周期均无指标，变化行全部为零。",
    "warning.previous_no_metrics": "上一周期没有指标，因此变化无法判定为显著。",
    "warning.no_provider_rows": "所有提供商均未返回数据，流量与优化机会为空。",
    "generic.no_sections": "此报告没有章节。",
    "generic.other_metrics": "其他指标",
    "generic.field": "字段",
    "generic.value": "值",
    "telegram.truncated": "... [已截断：完整内容见邮件/HTML 报告]",
    # Footer
    "footer.generated_by": "由 lv24-seo 生成",
    "footer.property_id": "资源 ID：{property_id}",
    "footer.license": "Apache-2.0 许可",
    # Subjects
    "subject.weekly": "每周 SEO 报告：{period}",
    "subject.no_data": "无搜索数据：{period}",
    "subject.no_comparison": "无可比数据的 SEO 报告：{period}",
    "subject.critical": "严重 SEO 问题：{count} 个",
    "subject.clicks": "点击次数：{change}",
    "subject.impressions": "展示次数：{change}",
    "subject.ctr": "点击率：{change}",
    "subject.position": "平均排名：{change}",
    # Units
    "unit.clicks": "点击次数",
    "unit.impressions": "展示次数",
    "unit.rows": "行",
    "unit.sessions": "会话数",
    "unit.positions": "个位置",
    "unit.ms": "毫秒",
    "unit.pp": "个百分点",
}

_CATALOGS: dict[str, dict[str, str]] = {
    "en": _EN_CATALOG,
    "ru": _RU_CATALOG,
    "zh": _ZH_CATALOG,
}

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# C0 controls, DEL, bidi embeddings/isolates/marks and invisible separators.
_PARAM_STRIP_CODES: tuple[int, ...] = (
    *range(0x20),
    0x7F,
    0x061C,
    0x200E,
    0x200F,
    0x2028,
    0x2029,
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
    0xFEFF,
)
_PARAM_STRIP: dict[int, None] = dict.fromkeys(_PARAM_STRIP_CODES)


def normalize_locale(value: str | None) -> str:
    """Return a supported locale for *value*, never raising.

    ``None``, an empty string and unknown locales become ``"en"``. Language
    subtags are ignored: ``en-*`` becomes ``en`` and ``ru-*`` becomes ``ru``.
    Simplified Chinese (``zh``, ``zh-CN``, ``zh-SG``, ``zh-Hans`` and their
    subtags) becomes ``zh``; Traditional Chinese (``zh-TW``, ``zh-HK``,
    ``zh-Hant``) and unknown Chinese variants become ``en`` because no
    Traditional catalog ships yet.
    """
    if not isinstance(value, str):
        return DEFAULT_LOCALE
    parts = [part for part in value.strip().lower().replace("_", "-").split("-") if part]
    if not parts:
        return DEFAULT_LOCALE
    primary = parts[0]
    if primary in ("en", "ru"):
        return primary
    if primary != "zh":
        return DEFAULT_LOCALE
    subtags = parts[1:]
    if not subtags or subtags == ["cn"] or subtags == ["sg"] or subtags == ["hans"]:
        return "zh"
    if subtags == ["hans", "cn"] or subtags == ["hans", "sg"]:
        return "zh"
    return DEFAULT_LOCALE


def _param_text(value: object) -> str:
    """Render one substitution value literally, dropping control characters."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text.translate(_PARAM_STRIP)


def _interpolate(template: str, params: Mapping[str, object]) -> str:
    """Substitute ``{name}`` placeholders in a single pass.

    Replacement values are never rescanned, so a parameter that contains
    ``{x}`` keeps it literally. Unknown placeholders become an empty string.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return ""
        return _param_text(params[name])

    return _PLACEHOLDER_RE.sub(replace, template)


def _as_decimal(value: object) -> Decimal | None:
    """Parse a finite number without raising; booleans and junk are invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(repr(value))
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def _valid_digits(digits: object) -> bool:
    return isinstance(digits, int) and not isinstance(digits, bool) and digits >= 0


def _plural_form(locale: str, n: int) -> str:
    """Return the CLDR plural category used by the shipped catalogs."""
    value = abs(n)
    if locale == "en":
        return "one" if value == 1 else "other"
    if locale == "ru":
        if value % 10 == 1 and value % 100 != 11:
            return "one"
        if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
            return "few"
        return "many"
    return "other"


def _as_date(value: object) -> date | None:
    """Coerce a date, datetime or ISO-8601 string to a date, never raising."""
    if isinstance(value, datetime):
        moment = _as_datetime(value)
        return moment.date() if moment is not None else None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        moment = _as_datetime(text)
        return moment.date() if moment is not None else None


def _as_datetime(value: object) -> datetime | None:
    """Coerce a date, datetime or ISO-8601 string to UTC, never raising."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _group_digits(text: str, separator: str) -> str:
    """Group an unsigned integer string in threes."""
    if len(text) <= 3:
        return text
    head = len(text) % 3 or 3
    parts = [text[:head]]
    parts.extend(text[index : index + 3] for index in range(head, len(text), 3))
    return separator.join(parts)


def _round(value: Decimal, digits: int) -> Decimal | None:
    """Round half-up and normalize negative zero; ``None`` when out of range."""
    quantum = Decimal(1).scaleb(-digits)
    try:
        rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if rounded == 0:
        rounded = abs(rounded)
    return rounded


class Translator:
    """Locale-bound catalog lookup and deterministic formatting."""

    __slots__ = ("html_lang", "locale")

    locale: str
    html_lang: str

    def __init__(self, locale: str | None = None) -> None:
        self.locale = normalize_locale(locale)
        self.html_lang = _HTML_LANG[self.locale]

    def __repr__(self) -> str:
        return f"Translator({self.locale!r})"

    def _lookup(self, key: str) -> str | None:
        template = _CATALOGS[self.locale].get(key)
        if template:
            return template
        template = _CATALOGS[DEFAULT_LOCALE].get(key)
        return template or None

    def t(self, key: str, /, **params: object) -> str:
        """Return the localized template for *key* with ``{name}`` params.

        A missing localized key falls back to English, then to *key* itself.
        Extra parameters are ignored and missing ones render as an empty string.
        """
        if not isinstance(key, str) or not key:
            return ""
        template = self._lookup(key)
        if template is None:
            return key
        return _interpolate(template, params)

    def count(self, key: str, n: int, /, **params: object) -> str:
        """Return the plural form of *key* for *n* with ``{count}`` filled in.

        Russian uses ``one``/``few``/``many``; English uses ``one``/``other``;
        Chinese uses ``other``. ``{count}`` is locale-formatted (grouping and
        decimal separator), and explicit ``count`` parameters cannot override it.
        """
        if not isinstance(key, str) or not key:
            return ""
        amount = n if isinstance(n, int) and not isinstance(n, bool) else 0
        form = _plural_form(self.locale, amount)
        template = _CATALOGS[self.locale].get(f"{key}.{form}")
        if not template:
            template = _CATALOGS[DEFAULT_LOCALE].get(f"{key}.{form}")
        if not template:
            template = _CATALOGS[DEFAULT_LOCALE].get(f"{key}.other")
        if not template:
            return key
        values: dict[str, object] = dict(params)
        values["count"] = self.number(amount)
        return _interpolate(template, values)

    def _format_rounded(self, rounded: Decimal, digits: int) -> str:
        text = f"{rounded:f}"
        sign = ""
        if text.startswith("-"):
            sign = "-"
            text = text[1:]
        if "." in text:
            integer, fraction = text.split(".", 1)
        else:
            integer, fraction = text, ""
        group_separator, decimal_separator = _NUMBER_FORMATS[self.locale]
        grouped = _group_digits(integer, group_separator)
        if digits:
            return f"{sign}{grouped}{decimal_separator}{fraction}"
        return f"{sign}{grouped}"

    def number(self, value: object, digits: int = 0) -> str:
        """Format a finite number with grouping and half-up rounding.

        Invalid values (``None``, NaN, infinities, booleans, non-numeric
        strings) return an empty string. Negative zero is never emitted.
        """
        amount = _as_decimal(value)
        if amount is None or not _valid_digits(digits):
            return ""
        rounded = _round(amount, int(digits))
        if rounded is None:
            return ""
        return self._format_rounded(rounded, int(digits))

    def percent(
        self,
        value: object,
        *,
        signed: bool = False,
        digits: int = 1,
        scale: str = "percent",
    ) -> str:
        """Format a percentage; ``scale="ratio"`` turns 0..1 into 0..100.

        A positive value gets ``+`` only when *signed* is true. Negative values
        always use the ASCII hyphen. Russian places a no-break space before
        ``%``. Rounded-to-zero values never carry a sign.
        """
        amount = _as_decimal(value)
        if amount is None or not _valid_digits(digits):
            return ""
        if isinstance(scale, str) and scale.strip().lower() == "ratio":
            amount *= 100
        rounded = _round(amount, int(digits))
        if rounded is None:
            return ""
        prefix = ""
        if rounded > 0 and signed:
            prefix = "+"
        elif rounded < 0:
            prefix = "-"
        body = self._format_rounded(abs(rounded), int(digits))
        suffix = "\u00a0%" if self.locale == "ru" else "%"
        return f"{prefix}{body}{suffix}"

    def position(self, value: object) -> str:
        """Format an average position with at most one decimal, no trailing .0."""
        amount = _as_decimal(value)
        if amount is None:
            return ""
        rounded = _round(amount, 1)
        if rounded is None:
            return ""
        body = self._format_rounded(rounded, 1)
        decimal_separator = _NUMBER_FORMATS[self.locale][1]
        if body.endswith(f"{decimal_separator}0"):
            body = body[:-2]
        return body

    def date(self, value: object) -> str:
        """Format a human date without ``strftime`` or the system locale.

        ``en`` -> ``2026-11-08``, ``ru`` -> ``08.11.2026``,
        ``zh`` -> ``2026年11月8日``. Invalid values return an empty string.
        """
        day = _as_date(value)
        if day is None:
            return ""
        if self.locale == "en":
            return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"
        if self.locale == "ru":
            return f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
        return f"{day.year}年{day.month}月{day.day}日"

    def datetime(self, value: object) -> str:
        """Format a human UTC timestamp as date plus ``HH:MM UTC``.

        English uses ``10 Nov 2026, 06:00 UTC`` so mail footers never show an
        ISO timestamp; Russian and Chinese reuse their human date form.
        """
        moment = _as_datetime(value)
        if moment is None:
            return ""
        clock = f"{moment.hour:02d}:{moment.minute:02d} UTC"
        if self.locale == "en":
            month = _EN_MONTHS[moment.month - 1]
            return f"{moment.day} {month} {moment.year}, {clock}"
        return f"{self.date(moment.date())} {clock}"

    def _short_date(self, day: _Date) -> str:
        if self.locale == "en":
            return f"{day.day} {_EN_MONTHS[day.month - 1]}"
        if self.locale == "ru":
            return f"{day.day} {_RU_MONTHS[day.month - 1]}"
        return f"{day.month}月{day.day}日"

    def _range(self, start: _Date, end: _Date, *, short: bool) -> str:
        if start == end:
            return self._short_date(start) if short else self.date(start)
        if start > end:
            start, end = end, start
        same_year = start.year == end.year
        same_month = same_year and start.month == end.month
        if self.locale == "en":
            months = _EN_MONTHS
        elif self.locale == "ru":
            months = _RU_MONTHS
        else:
            months = ()
        if months:
            first = months[start.month - 1]
            last = months[end.month - 1]
            if same_month:
                body = f"{start.day}\u2013{end.day} {first}"
                return body if short else f"{body} {start.year}"
            if same_year:
                body = f"{start.day} {first} \u2013 {end.day} {last}"
                return body if short else f"{body} {start.year}"
            return f"{start.day} {first} {start.year} \u2013 {end.day} {last} {end.year}"
        if same_month:
            body = f"{start.month}月{start.day}\u2013{end.day}日"
            return body if short else f"{start.year}年{body}"
        if same_year:
            body = f"{start.month}月{start.day}日\u2013{end.month}月{end.day}日"
            return body if short else f"{start.year}年{body}"
        return (
            f"{start.year}年{start.month}月{start.day}日\u2013{end.year}年{end.month}月{end.day}日"
        )

    def period(self, start: object, end: object) -> str:
        """Format a human date range, for example ``3-9 Nov 2026``."""
        first = _as_date(start)
        last = _as_date(end)
        if first is None and last is None:
            return ""
        if first is None:
            return self.date(last)
        if last is None:
            return self.date(first)
        return self._range(first, last, short=False)

    def short_period(self, start: object, end: object) -> str:
        """Format a compact date range for subject lines.

        The year is omitted when both endpoints share it; cross-year ranges
        keep both years to stay unambiguous.
        """
        first = _as_date(start)
        last = _as_date(end)
        if first is None and last is None:
            return ""
        if first is None:
            return self._short_date(last) if last is not None else ""
        if last is None:
            return self._short_date(first)
        return self._range(first, last, short=True)


def translator(locale: str | None = None) -> Translator:
    """Return a :class:`Translator` for *locale*, defaulting to English."""
    return Translator(locale)
