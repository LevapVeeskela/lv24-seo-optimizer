# lv24-seo-optimizer

**Бесплатный SEO-пульт для людей и ИИ-агентов.**

Собирает Google Search Console, Яндекс.Вебмастер, Яндекс.Метрику, Bing/IndexNow, CrUX и
PageSpeed Insights в одну нормализованную модель, краулит сайт на технические регрессии и отдаёт
это как **письма по расписанию**, **MCP-инструменты**, **CLI** и **скиллы для агентов**.

Платные SEO-платформы не нужны: весь базовый путь работает на бесплатных тарифах.

## Что внутри

| Слой | Что даёт |
|---|---|
| Источники | GSC, Вебмастер, Метрика, Bing/IndexNow, CrUX, PSI, собственный краул, логи nginx |
| Отчёты | недельный дайджест с дельтами, триаж индексации, « Striking distance», деградация контента, техрегрессии |
| MCP | 8–12 стабильных инструментов, по умолчанию только чтение |
| CLI | `init`, `doctor`, `auth`, `gsc`, `yandex`, `crawl`, `audit`, `report`, `schedule`, `mcp serve` |
| Скиллы | `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit` |
| Автоматизация | cron / systemd timer / Планировщик Windows, GitHub Actions, приём логов nginx |

## Статус

**Pre-alpha**: репозиторий и план. План реализации —
[`docs/plans/2026-11-10-implementation-plan.md`](docs/plans/2026-11-10-implementation-plan.md),
обзор рынка и лимитов API — [`docs/research/`](docs/research/).

## Принципы

1. **Ноль расходов** — бесплатные тарифы, SQLite, SMTP/Telegram, публичный CI.
2. **Только чтение по умолчанию** — отправка sitemap, переобход и IndexNow требуют явного `--allow-write`.
3. **Честность данных** — в каждом ответе `truncated`, `total`, `data_age`, `limitations[]`.
4. **Один источник правды** — реестр инструментов генерирует MCP-схемы, CLI и документацию.

## Для агентов

Начните с [`AGENTS.md`](AGENTS.md). Машиночитаемые файлы: `schemas/tools.json`, `llms.txt`,
`skills/*/SKILL.md`.

## Лицензия

Apache-2.0. Данные Google/Яндекса принадлежат владельцу ресурса; проект их не перепродаёт.
