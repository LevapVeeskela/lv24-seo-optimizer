# lv24-seo-optimizer — план реализации (роевая разработка)

Дата: 2026-11-10 · Статус: **утверждаемый план** · Репозиторий: `github.com/LevapVeeskela/lv24-seo-optimizer`
Связанные документы: [архитектура](../architecture.md) · [источники и лимиты](../data-sources.md) ·
[MCP](../mcp.md) · [CLI](../cli.md) · [конфиг](../config.md) · [расписание](../scheduling.md) ·
[скиллы](../skills.md) · [исследование 2026](../research/2026-11-landscape.md)

---

## 0. Цель и критерии приёмки

**Цель.** Публичный open-source инструмент, который закрывает весь цикл SEO-контроля для владельца
сайта без платных сервисов: собрать данные из бесплатных API → сохранить историю → найти регрессии и
возможности → отдать человеку письмом, агенту — через MCP/CLI/скилл.

**Критерии приёмки v0.1.0 (первый публичный релиз):**

1. `uvx --from lv24-seo lv24-seo doctor` работает на чистой машине Windows/macOS/Linux без ключей
   (честно показывает, чего не хватает).
2. `lv24-seo init` + `lv24-seo auth login --provider gsc|yandex` → `lv24-seo report weekly` строит отчёт
   по реальному сайту из GSC + Вебмастера + Метрики.
3. Отчёт уходит письмом по SMTP и в Telegram (critical-only), повторная отправка за тот же период
   блокируется идемпотентностью.
4. `lv24-seo mcp serve` регистрируется в Claude Desktop / OpenCode / Cursor и отдаёт 10 инструментов
   с честными `truncated/total/data_age/limitations`.
5. `lv24-seo audit --start https://…` находит ≥30 правил, из них ≥10 critical/high, с фикстурами.
6. CI зелёный на публичном репозитории, расходы GitHub = $0.
7. Документация: README (en+ru), AGENTS.md, llms.txt, 3 скилла, docs/*, CONTRIBUTING, SECURITY.

**Не-цели (жёстко):** платные тарифы в базовом пути; скрейпинг выдачи Google/Яндекса; автоматизация
карточки Яндекс.Бизнеса и ответов на отзывы; генерация контента «в массовом порядке»; бэклинк-анализ
без ключа пользователя; собственный рендер-фермы (Rendertron) — только проверка паритета на выборке.

---

## 1. Продуктовые принципы (нарушение = блокер ревью)

| # | Принцип | Как проверяем |
|---|---|---|
| P1 | **Ноль расходов** | CI только `ubuntu-latest`, двойной Python-матрикс; ни одной зависимости с обязательной картой; отчёт `doctor` печатает, какие источники бесплатны |
| P2 | **Честность данных** | Каждый ответ провайдера несёт `truncated/total/data_age_days/limitations[]`; тест на каждое усечение |
| P3 | **Read-only по умолчанию** | Все write-операции за `--allow-write` + confirm token + idempotency key; e2e-тест «без флага ничего не отправлено» |
| P4 | **Один источник правды** | `mcp/registry.py` → схемы/CLI-хелп/доки/`schemas/tools.json`; CI падает на дрейфе |
| P5 | **Агент-нативность** | CLI `--json` везде, стабильные exit-коды, `tools.json` без MCP, скиллы с деревом решений |
| P6 | **Кроссплатформенность** | Тесты путей/кодировок; Windows-джоб в nightly, не в PR |
| P7 | **Диффы, а не дашборды** | Отчёт содержит блок «что изменилось»; письмо не уходит без материальных изменений (`only_on_change`) |

---

## 2. Ключевые решения (фиксируем до старта)

| ID | Решение | Обоснование | Альтернатива |
|---|---|---|---|
| D1 | Python 3.11+ / uv / hatchling | один язык для MCP, CLI, краулера, отчётов; `uvx` = one-liner установка | TS — дублирование краулера и шаблонов |
| D2 | MCP: stdio по умолчанию, опционально stateless Streamable HTTP | локальные токены, ничего не хостим; SSE устарел | HTTP-only — требует хостинга и секретов на сервере |
| D3 | 10 инструментов, композитные `seo_*` вместо зеркала API | агент выбирает лучше; меньше схемного шума | 50 инструментов-обёрток |
| D4 | SQLite WAL + gzip-JSONL сырьё (90 дней) | ноль инфраструктуры, достаточно для 1–50 сайтов | Postgres/DuckDB — лишний вес |
| D5 | SMTP по умолчанию, Resend/Brevo адаптеры | работает на VPS, без вендора | только API-провайдер |
| D6 | Apache-2.0 | патентная оговорка, дружелюбно к корпорациям | MIT |
| D7 | Typer + JSON-конверт `{ok,data,meta,warnings}` | одинаково удобно человеку и агенту | argparse/click |
| D8 | Ключи: keychain → env → 0600-файл | по умолчанию ничего не течёт в репо | только `.env` |

ADR-файлы: `docs/decisions/ADR-0001-stack.md`, `ADR-0002-honesty-envelope.md`,
`ADR-0003-write-gating.md` (создаются в WP0.1).

---

## 3. Карта волн и зависимостей

```
W0 Foundation ──┬─▶ W1 Providers ──┬─▶ W2 Store ──┬─▶ W4 Analysis ──┬─▶ W5 Reports ──▶ W6 CLI ─┐
                │                  │              │                 │                          ├─▶ W9 Hardening ─▶ W10 Release
                └─▶ W3 Crawl ──────┘              └─────────────────┘                          │
                                                    W7 MCP ◀── W1/W2/W4 (registry) ────────────┘
                                                    W8 Automation/Integrations ◀── W5/W6
```

Критический путь: **W0 → W1 → W2 → W4 → W5 → W6 → W9 → W10** (≈ 34 агент-дня).
Параллельные ветки: W3 (краул) стартует сразу после W0; W7 (MCP) — после W2+W4; W8 — после W5.

---

## 4. Work packages

Обозначения: **S** ≤ 0.5 дня, **M** 1–2 дня, **L** 3–5 дней (в агент-днях одного сильного агента).
Роль: `arch` архитектор/ревьюер, `impl` реализатор, `test` тест-инженер, `docs` документалист.

### W0 — Foundation (5–6 дней, параллельно 4 агента)

| ID | Размер | Роль | Что сделать | Файлы | DoD |
|---|---|---|---|---|---|
| WP0.1 | S | arch | ADR-0001..0003, финализация решений D1–D8 | `docs/decisions/*` | 3 ADR, ссылки из AGENTS.md |
| WP0.2 | M | impl | `core/config.py`: загрузка `seo.toml` + env, приоритеты, валидация pydantic, `config path/show` | `src/lv24_seo/core/config.py`, тесты | 12+ тестов приоритетов; ошибка при неизвестном ключе |
| WP0.3 | M | impl | `core/secrets.py`: keychain, env, 0600-файл, `auth status` без утечки токена | `core/secrets.py` | тест: токен не попадает в repr/логи |
| WP0.4 | M | impl | `core/http.py`: httpx-клиент, retry (идемпотентные), `Retry-After`, таймауты, IPv4-форс, UA | `core/http.py` | тесты на 429/503/таймаут, `family=4` |
| WP0.5 | S | impl | `core/ratelimit.py`: token bucket per provider/method | `core/ratelimit.py` | тест: 200 req/5min не превышается |
| WP0.6 | S | impl | `core/errors.py` + `core/envelope.py`: коды, конверт, exit-коды | `core/*` | тест маппинга код→exit |
| WP0.7 | S | impl | `core/logging.py`: redaction-фильтр, JSON-логи, уровни | `core/logging.py` | тест: `Authorization`, `token`, cookie вырезаны |
| WP0.8 | S | test | Харнесс тестов: фикстуры, `pytest`-маркеры `live/network`, фабрики | `tests/conftest.py` | `pytest -q` без сети |
| WP0.9 | S | docs | `docs/guides/quickstart.md` (en) + `quickstart.ru.md` | `docs/guides/*` | команды копипастятся и работают |

**Выход волны:** `lv24-seo doctor --json` честно отчитывается; секреты и HTTP-слой готовы для провайдеров.

### W1 — Провайдеры, только чтение (7–8 дней, 5–6 агентов параллельно)

Контракт один: `ProviderResult[T]` (см. architecture §2). Каждый провайдер = отдельный пакет
`providers/<name>/{client,models,adapter}.py` + фикстуры.

| ID | Размер | Провайдер | Ключевые методы | DoD |
|---|---|---|---|---|
| WP1.1 | M | gsc | `searchAnalytics.query`, `sites.list`, `sitemaps.list` | пагинация до 50k, `truncated`, группировки page/query раздельно |
| WP1.2 | M | gsc | `urlInspection.index:inspect` | 1 URL/вызов, очередь, кэш 24ч, 2000/день |
| WP1.3 | L | yandex_webmaster | `/user/`, `/hosts/`, `summary`, `indexing/history`, `search-urls/*`, `diagnostics` | резолв `host_id`, 401 vs 403, пустая история = `degraded` |
| WP1.4 | M | yandex_webmaster | `search-queries/popular`, `all/history`, `query-analytics/list` | repeated params, top-3000, `no_query_page_join` |
| WP1.5 | M | yandex_metrika | `/stat/v1/data`, `bytime`, goals, `management/v1/counters` | sampling-флаг, 200 req/5 мин, `full` опционально |
| WP1.6 | S | bing + indexnow | `GetQueryStats`, `GetCrawlIssues`, IndexNow POST | free-tier лимиты, key file проверка |
| WP1.7 | S | crux + psi | `queryRecord`, `runPagespeed` | 404 CrUX = «мало трафика», а не ошибка |
| WP1.8 | M | ga4 (опц.) | `runReport` organic landing pages | выключен без токена, thresholding помечен |
| WP1.9 | M | test | Контрактные тесты: записанные ответы → модель → снимок | 6 провайдеров × 3 сценария (ok/пусто/ошибка) |
| WP1.10 | S | docs | Обновить `docs/data-sources.md` фактическими кодами лимитов | таблица совпадает с кодом |

**Правила волны:** ни один адаптер не пишет в БД; ни один адаптер не логирует тело ответа на INFO;
все ограничения — константы в `providers/<name>/limits.py` и попадают в `limitations[]`.

### W2 — Хранилище и базлайн (4–5 дней, 2 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP2.1 | M | impl | Схема SQLite (9 таблиц), миграции `store/migrations/*.sql`, PRAGMA WAL | `store init` идемпотентен |
| WP2.2 | M | impl | Репозитории: upsert метрик, страниц, issue, snapshots, sends | тесты на конфликты/повторы |
| WP2.3 | S | impl | Raw-хранилище `data/raw/<source>/<date>/*.json.gz` + retention 90д | тест prune |
| WP2.4 | S | impl | `store/base.py`: снимок периода для диффа (хэш + gzip payload) | повторный запуск не создаёт дубль |
| WP2.5 | M | test | Тесты миграций с нуля и с предыдущей версии | 2 версии → одинаковый результат |

### W3 — Краулер и правила (6–7 дней, 3–4 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP3.1 | M | impl | `crawl/fetcher.py`: httpx, robots.txt (RFC 9309), concurrency, таймауты, ретраи | соблюдает Disallow, лимит 4 |
| WP3.2 | S | impl | `crawl/sitemap.py`: парсер XML/индексов, лимиты 50k/50MB, gzip | валидация + список URL |
| WP3.3 | M | impl | `crawl/html.py`: title, meta robots, canonical, hreflang, JSON-LD, ссылки, img alt | извлечение без браузера |
| WP3.4 | M | impl | `crawl/graph.py`: граф внутренних ссылок, орфаны, глубина, битые ссылки | корректно на 1000 URL |
| WP3.5 | L | impl | 30+ правил в `crawl/rules/*` (по одному файлу), severity-модель | каждое правило: violation + clean фикстура |
| WP3.6 | S | impl | `crawl/render_check.py`: паритет shell vs hydrated (опционально Playwright) | 3 шаблона, деградация без браузера |
| WP3.7 | M | test | e2e-краул на локальном статическом сайте-фикстуре | 0 сети, детерминированный отчёт |

**Приоритет правил (реализовать первыми):** noindex на canonical, robots Disallow, canonical conflict,
sitemap URL ≠ 200/canonical, redirect chain, soft-404, JSON-LD required, duplicate title, hreflang
reciprocity, TLS expiry, 5xx/429 для ботов, alt coverage, orphan pages.

### W4 — Анализ (5–6 дней, 3 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP4.1 | M | impl | `analysis/delta.py`: пороги (клики 10%, показы 15%, позиция 1.5), проверка значимости | тест на шум/значимость |
| WP4.2 | M | impl | `analysis/opportunities.py`: striking distance, CTR gap, decay | ранжирование + evidence |
| WP4.3 | M | impl | `analysis/indexing.py`: бакеты причин (GSC coverage + Yandex events + crawl) | 8 бакетов, next_action |
| WP4.4 | M | impl | `analysis/fusion.py`: склейка движков без фейкового query×page | отчёт помечает источник |
| WP4.5 | M | impl | `analysis/logs.py`: nginx JSON → bot_daily, crawl waste, 5xx ботам | верификация бота по IP/PTR |
| WP4.6 | S | test | Снапшот-тесты отчётов на синтетических данных | стабильный JSON |

### W5 — Отчёты и доставка (5–6 дней, 3 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP5.1 | M | impl | `report/builders/weekly.py` (headline, deltas, issues, opportunities, honesty-блок) | JSON-схема отчёта |
| WP5.2 | M | impl | Рендеры `text/html/md` из одного объекта, inline CSS, без пикселей | письмо в Gmail/Outlook не ломается |
| WP5.3 | M | impl | SMTP-доставка + идемпотентность `sends` + `only_on_change` | повтор не отправляет |
| WP5.4 | S | impl | Telegram critical-only, усечённый текст | тест форматирования |
| WP5.5 | S | impl | `report daily/indexing/opportunities` | 3 отчёта на синтетике |
| WP5.6 | M | impl | Генераторы расписаний: cron, systemd user units, Task Scheduler XML | `--print` + идемпотентная установка |

### W6 — CLI (4–5 дней, 2 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP6.1 | M | impl | Полное дерево команд (см. `docs/cli.md`), конверт, exit-коды | `--json` на каждой команде |
| WP6.2 | S | impl | `init`, `auth login/status/logout`, `properties`, `config` | loopback OAuth Google, device/manual Yandex |
| WP6.3 | S | impl | `install --client …` (claude/opencode/vscode/cursor) | пишет корректный JSON, идемпотентно |
| WP6.4 | S | impl | `export tools`, `--env-file`, `--secrets-file` | CI-дрейф зелёный |
| WP6.5 | M | test | Golden-тесты справки и JSON-конверта | снапшоты в репо |

### W7 — MCP-сервер (5–6 дней, 2 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP7.1 | M | impl | `mcp/server.py`: stdio + `--transport http --stateless`, lifecycle | MCP Inspector подключается |
| WP7.2 | M | impl | 8 read-инструментов из реестра, компактный JSON, `format=md` | схемы = `schemas/tools.json` |
| WP7.3 | M | impl | 2 write-инструмента: dry-run → confirm_token → apply, аудит | без токена — отказ `CONFIRM_REQUIRED` |
| WP7.4 | S | impl | Resources (`seo://config`, `seo://report/latest`, baseline) + 3 prompt | список виден в клиенте |
| WP7.5 | M | test | In-memory client тесты, снимок `tools/list`, ошибки/усечения | 20+ тестов |

### W8 — Автоматизация и интеграции (4–5 дней, 3 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP8.1 | S | impl | Рецепт GitHub Actions (cron + secrets + artifact), документирован | YAML валиден, $0 |
| WP8.2 | S | impl | `indexnow submit`, `bing` write, `gsc sitemap submit` (гейты) | dry-run по умолчанию |
| WP8.3 | M | impl | Опциональные адаптеры DataForSEO/SerpApi (BYO key, off by default) | без ключа — `provider_disabled` |
| WP8.4 | S | impl | Мульти-сайт (`[[sites]]`), объединённый дайджест | 2 сайта в одном письме |
| WP8.5 | S | docs | `docs/guides/github-actions.md`, `docs/guides/vps-cron.md` | проверено на тестовом репо |

### W9 — Хардненинг (5–6 дней, 4 агента)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP9.1 | M | test | Покрытие ядра ≥80%, мутационные проверки на критичных правилах | coverage-отчёт |
| WP9.2 | M | impl | Производительность: краул 1000 URL < 3 мин локально, память < 300 МБ | бенчмарк в CI (мягкий) |
| WP9.3 | M | impl | Безопасность: redaction-аудит, gitleaks, `--allow-write` e2e, path traversal в sitemap | SECURITY.md обновлён |
| WP9.4 | S | impl | Windows-совместимость: пути, кодировки, Task Scheduler | nightly Windows job зелёный |
| WP9.5 | M | docs | i18n отчётов ru/en, скриншоты, `docs/guides/*` | ссылки живые, markdown-lint |
| WP9.6 | S | impl | `lv24-seo doctor` проверяет DNS (SPF/DKIM/DMARC), TLS, sitemap, robots | 8 проверок с понятным выводом |

### W10 — Релиз (4–5 дней, 2 агента + arch)

| ID | Размер | Роль | Что | DoD |
|---|---|---|---|---|
| WP10.1 | S | impl | PyPI trusted publishing (OIDC), версия 0.1.0 | `uvx --from lv24-seo lv24-seo version` |
| WP10.2 | S | impl | `server.json`, публикация в MCP Registry, README-бейджи | `mcp-publisher validate` ok |
| WP10.3 | S | impl | Docker-образ `ghcr.io/…` (опционально, для VPS) | multi-arch, ≤200 МБ |
| WP10.4 | M | docs | Релизные заметки, `CHANGELOG`, 3 примера (lv24.by, статический сайт, SPA) | примеры запускаются |
| WP10.5 | S | arch | Финальное ревью DoD v0.1, тег, анонс | чеклист §9 закрыт |

---

## 5. Роевая стратегия

### 5.1 Роли

| Роль | Модель (рекомендация) | Ответственность |
|---|---|---|
| **Architect / parent** | сильная (оркестратор) | план, контракты, ревью, merge, DoD, финальные решения |
| **Impl-agent** | быстрая/дешёвая | один WP, узкий scope, тесты к нему |
| **Test-agent** | быстрая | контрактные/e2e фикстуры, снапшоты |
| **Docs-agent** | быстрая | доки/примеры строго по коду |
| **Reviewer-agent** | сильная | независимое ревью: P1–P7, утечки, лимиты |

Правило: **parent не пишет продовый код волны**, кроме интеграционных склеек < 30 строк.

### 5.2 Оси параллелизма (без конфликтов)

1. **По провайдеру** — W1: 6 независимых пакетов, разные файлы.
2. **По правилу краула** — W3.5: один агент = 4–6 правил, по файлу на правило.
3. **По отчёту** — W5: weekly / indexing / opportunities.
4. **По поверхности** — CLI (W6) и MCP (W7) параллельно после W4.
5. **По документации** — docs-agent всегда отдельно, но merge после кода.

### 5.3 Владение файлами (защита от конфликтов)

* Один файл — один владелец в волне. Файлы общего контракта (`core/envelope.py`,
  `mcp/registry.py`, `store/schema.sql`) меняет **только architect** через PR-«contract change».
* Агент не трогает чужие пакеты; нужен общий хелпер — заявка architect'у, тот добавляет в `core/`.
* Ветка на WP: `wp/<id>-<slug>`; merge в `wave/<n>` squash-коммитом; `wave/<n>` → `main` после
  ревью волны.

### 5.4 Промпт-шаблон для impl-агента

```
Ты реализуешь WP<id> в репозитории lv24-seo-optimizer.
Прочитай AGENTS.md, docs/architecture.md, docs/<area>.md и план §4 (свой WP).
Scope: только файлы <list>. Не меняй core-контракты и mcp/registry.py.
Требования:
- тесты на happy path + минимум 2 failure mode;
- никаких новых зависимостей без обоснования;
- лимиты провайдера — в limitations[], не в комментариях;
- всё, что печатается, проходит redaction.
Выход: список изменённых файлов, команды проверки, что осталось за рамками WP.
```

### 5.5 Промпт-шаблон для reviewer-агента

```
Ты ревьюишь diff WP<id>. Проверь по чеклисту P1–P7 и §1:
1) есть ли путь, где данные усечены, но truncated=false?
2) есть ли write без --allow-write/confirm_token?
3) есть ли секрет в логах/repr/ошибке?
4) выдержаны ли лимиты провайдера (числа из docs/data-sources.md)?
5) работает ли на Windows (paths, encoding, CRLF)?
6) тесты падают, если убрать фикс?
Верни: BLOCKERS / SHOULD-FIX / NITS, каждый с файлом и строкой.
```

### 5.6 Гейты качества по волне

| Гейт | Когда | Кто |
|---|---|---|
| G1 Линт/типы/тесты | каждый PR | CI |
| G2 Контракт провайдера | W1, W8 | reviewer |
| G3 Честность/усечение | W1, W4, W5 | reviewer + тест |
| G4 Write-гейт | W5, W7, W8 | reviewer + e2e |
| G5 Доки | конец волны | docs-agent |
| G6 Дрейф схем | каждый PR | CI `schemas` |
| G7 Релизный DoD | W10 | architect |

---

## 6. Тестовая стратегия

| Уровень | Что | Где | В CI |
|---|---|---|---|
| Unit | парсеры, правила, диффы, лимиты | `tests/unit` | да |
| Contract | записанные ответы API → модель → снимок | `tests/contract` | да |
| E2E offline | локальный статический сайт (fixtures) + фейковый API | `tests/e2e` | да |
| Live | реальные GSC/Яндекс (по токенам) | маркер `live` | **нет** |
| Nightly | Windows + 3.13, полный краул фикстуры | GH Actions schedule | да, $0 |

Минимальные требования: новое правило краула — 2 фикстуры; новый провайдер — 3 сценария
(ok/пусто/ошибка+квота); новый write — тест «без флага ничего не произошло».

---

## 7. Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|
| Лимиты API меняются | средняя | высокий | константы в `limits.py`, `docs/data-sources.md`, ревизия перед релизом |
| Яндекс отдаёт `HOST_NOT_LOADED` | высокая | средний | статус `degraded`, отчёт без падения, повтор позже |
| GSC 50k-усечение искажает выводы | высокая | высокий | `truncated` + раздельные группировки + тест |
| OAuth-приложение Google в Testing → RT живёт 7 дней | средняя | высокий | `doctor` предупреждает; инструкция «publish app» |
| Токены Яндекса без refresh (6 мес) | высокая | средний | `auth status` + напоминание за 14 дней |
| Кросс-платформенные баги (Windows) | средняя | средний | nightly Windows, pathlib, utf-8 везде |
| Публичный репозиторий → утечка токена в issue | низкая | высокий | шаблоны issue, gitleaks, redaction, SECURITY.md |
| «Ещё один SEO-инструмент» без отличий | средняя | высокий | позиционирование: free-first fusion + diffs + honesty |
| Рост скоупа (бэклинки, AI-контент) | высокая | средний | не-цели §0, ADR на расширение |

---

## 8. Оценка и график (агент-дни, 4–6 параллельных агентов)

| Волна | Дни (один агент) | Параллельно | Календарно при 4 агентах |
|---|---|---|---|
| W0 | 6 | 4 | 2 |
| W1 | 8 | 5 | 2.5 |
| W2 | 5 | 2 | 2.5 |
| W3 | 7 | 3 | 2.5 |
| W4 | 6 | 3 | 2 |
| W5 | 6 | 3 | 2 |
| W6 | 5 | 2 | 2.5 |
| W7 | 6 | 2 | 3 |
| W8 | 5 | 3 | 2 |
| W9 | 6 | 4 | 2 |
| W10 | 5 | 2 | 2.5 |
| **Итого** | **65** | — | **≈ 25 календарных дней** |

Порядок запуска: `W0` → параллельно `W1` и `W3` → `W2` (после W1.1/W1.3) → `W4` → `W5` →
параллельно `W6` и `W7` → `W8` → `W9` → `W10`.

---

## 9. Чеклист готовности релиза v0.1.0

- [ ] Все 7 критериев §0 выполнены на реальном сайте (lv24.by) и на статическом примере
- [ ] `truncated/limitations` присутствуют в 100% ответов провайдеров (тест-скан)
- [ ] Ни одна write-операция не проходит без `--allow-write` и confirm-token (e2e)
- [ ] Токены не встречаются в логах/repr/трейсбеках (тест redaction)
- [ ] `schemas/tools.json` и `skills/manifest.json` синхронны (CI)
- [ ] README/AGENTS/llms.txt/скиллы/доки — без битых ссылок (`markdown-link-check`)
- [ ] CI: только публичные бесплатные минуты, artifacts ≤7 дней, SHA-pinned actions
- [ ] 3 скилла проходят «сухой прогон» с агентом-новичком (понятны без чтения кода)
- [ ] CHANGELOG + тег `v0.1.0` + релизные заметки
- [ ] MCP Registry: `server.json` валиден, пакет доступен через `uvx`

---

## 10. Что делаем прямо сейчас (первые 3 шага)

1. **WP0.1–WP0.3** (arch + impl): ADR, конфиг, секреты — разблокируют всех.
2. **WP1.1 + WP1.3** (2 агента): GSC и Яндекс — самый ценный сигнал; на них строится отчёт.
3. **WP3.1–WP3.3** (1–2 агента): краулер — независимая ветка, снимает главный техдолг.

После этого — ревью волны W0/W1, фиксация контрактов и запуск остальных.
