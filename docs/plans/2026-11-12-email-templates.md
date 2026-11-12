# Email templates + locales (2026-11-12)

Status: implementation contract. Orchestrator decision after recon.

## Goal

Product-grade weekly email (HTML + plain text + subject) with `en`, `ru`, `zh`.
No raw schema dumps (`metric:`, `current:`, `evidence: {...}`, booleans, rule ids).

## Decisions

1. **Canonical JSON stays English.** `Report.to_dict()`, snapshots, report diff, MCP
   payloads and machine codes (`metric`, `severity`, `engine`, `kind`, `status`) do not
   change. No `Report.locale` field. This avoids snapshot/diff/hash churn.
2. **Localization is presentation-only** in `report/i18n.py` + `report/render.py`.
   The weekly email presenter reads structured rows and ignores English builder
   `title`/`summary`/`notes`/`headline` for known weekly sections.
3. **Locale plumbing:** `report.locale` is the single source of truth for email, Telegram, CLI
   human output and `--out` HTML. `EmailConfig.locale` and `TelegramConfig.locale` are populated
   from it and default to `"en"` when delivery is called directly; there are no
   `report.email.locale` / `report.telegram.locale` keys in core config.
   `ReportConfig.locale` accepts `ru | en | zh`.
4. **Dedupe:** `content_hash(report, locale)` includes the normalized locale for `ru`/`zh` and
   omits it for `en`, preserving the pre-localization English digest so existing `sends` ledger
   rows do not cause a duplicate English send after upgrade. Switching to `ru`/`zh` re-sends the
   same period once.
5. **No external assets/scripts/styles/pixels.** Inline CSS, tables, text wordmark.

## Public API

`src/lv24_seo/report/i18n.py`

```python
SUPPORTED_LOCALES = ("en", "ru", "zh")
DEFAULT_LOCALE = "en"

def normalize_locale(value: str | None) -> str
def translator(locale: str | None = None) -> Translator

class Translator:
    locale: str
    html_lang: str            # en -> en, ru -> ru, zh -> zh-Hans
    def t(self, key: str, /, **params: object) -> str
    def count(self, key: str, n: int, /, **params: object) -> str
    def number(self, value: object, digits: int = 0) -> str
    def percent(self, value: object, *, signed: bool = False, digits: int = 1,
                scale: str = "percent") -> str   # scale="ratio" for 0..1 CTR
    def position(self, value: object) -> str
    def date(self, value: object) -> str          # human date
    def datetime(self, value: object) -> str      # human date + UTC time
    def period(self, start: object, end: object) -> str
    def short_period(self, start: object, end: object) -> str
```

Fallbacks: unknown locale -> `en`; missing key -> English; missing everywhere -> key;
invalid numeric/date -> empty string, never `0`/invented. No `strftime`, no system locale.

`src/lv24_seo/report/render.py`

```python
def render_text(report: Report, *, locale: str = "en") -> str          # 100 cols
def render_email_text(report: Report, *, locale: str = "en") -> str   # 78 cols
def render_html(report: Report, *, title: str | None = None, locale: str = "en") -> str
def render_subject(report: Report, *, locale: str = "en") -> str
def render_markdown(report: Report) -> str                            # unchanged English
```

`render_html`/`render_text` use the product weekly presenter for `kind == "weekly"`
(including fallback sections `gsc`, `yandex_webmaster`, `yandex_metrika`, `sources`).
Unknown kinds/sections keep a generic localized fallback, never crash.

`src/lv24_seo/report/delivery.py`

```python
EmailConfig(..., locale: str = "en")
TelegramConfig(..., locale: str = "en")
content_hash(report, locale: str = "en") -> str   # en keeps the legacy digest
deliver(..., locale: str = "en") -> list[DeliveryResult]  # locale = fallback only
send_email(...) uses cfg.locale + render_email_text/render_html/render_subject
send_telegram(...) uses cfg.locale + render_email_text
```

`services._deliver_report` passes `locale=self._config.report.locale` into both channel configs and
`deliver` (as a fallback for configs without a locale). `_weekly_report` adds `meta["locale"]`.
CLI `_report_lines` reads it and passes to `render_text`. `cli --out` already passes
`config.report.locale`.

## Product email structure

Header dark `#080D12`, neon `#5FD4C4` only on dark, 600px card, 20px side padding.
Order: preheader -> brand -> property/period/verdict -> KPI 2x2 -> opportunities
(top 3) -> issues -> sources (traffic merged) -> data notes -> warnings -> footer.
Critical issue => issues before opportunities. No separate traffic table.

KPI: Clicks, Impressions, CTR, Avg. position; current, signed delta, previous.
CTR ratio (0..1) -> percent; position 1 decimal. Unknown renders the localized "No data"
(`Нет данных` / `无数据`), never 0 or an em dash.
Delta color from `good`; position lower is better. No `significant` boolean printed;
localized "significant" badge only when true.

Opportunities: kind label, query, page, one evidence line, estimated clicks.
Ignore `evidence` dict and `rule_ids` list (show count only). Top 3.
Issues: counts by severity + unique rule count; empty state must say no data was
provided (not "site is clean").
Sources: ordered GSC, Yandex Webmaster, Yandex Metrica, Bing, CrUX, PSI, extras.
Metrica is sessions, not clicks; never show `0 clicks`.
Notes: `report.limitations`, per-engine limitations, warnings; dedupe; localized
headings. Provider prose stays original and is labelled as original in ru/zh.
Footer: generated timestamp UTC, exact property id, "generated by lv24-seo".

Subject priority: no data -> no comparison -> critical issues -> biggest significant
KPI -> default weekly. <=78 chars, no newline, localized; ASCII `-` for negatives.

## Locale formatting

- EN: `1,234.5`, `+18.4%`, `8.4`, `3–9 Nov 2026`, `2026-11-08`.
- RU: `1 234,5` (NBSP grouping), `+18,4 %`, `8,4`, `3–9 нояб. 2026`, `08.11.2026`.
- ZH: `1,234.5`, `+18.4%`, `8.4`, `2026年11月3–9日`.
- Plurals: EN one/other; RU one/few/many; ZH other + measure word.
- HTML `lang`: en/ru/zh-Hans; `dir="ltr"`; CJK font stack.
- Escape once at renderer boundary; strip control/bidi characters from params.

## Owned files

- i18n: `src/lv24_seo/report/i18n.py`, `tests/unit/test_report_i18n.py`.
- render: `src/lv24_seo/report/render.py`, `tests/unit/test_report_render.py`.
- plumbing: `src/lv24_seo/core/config.py`, `src/lv24_seo/report/delivery.py`,
  `src/lv24_seo/services.py`, `src/lv24_seo/cli/main.py`,
  `tests/unit/test_report_delivery.py`, `tests/unit/test_services.py`,
  `tests/unit/test_config.py`, `tests/unit/test_cli.py`,
  `tests/unit/test_delivery_regressions.py`.
- docs: `docs/config.md`, `docs/scheduling.md`, `docs/cli.md`, `docs/architecture.md`,
  `examples/config/seo.toml`, `CHANGELOG.md`, `docs/guides/mcp-clients.md`.

Do not edit files outside your package. Do not revert other agents' work. Do not bump
the version or publish. Final verification is the orchestrator's job.
