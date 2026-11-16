-- lv24-seo storage schema (Wave 2).
--
-- Contract:
--   * every DDL statement is idempotent (`IF NOT EXISTS`), so `initialize()` may run on every
--     process start and on both fresh and existing databases;
--   * timestamps are ISO-8601 UTC strings (`2026-11-10T09:30:00+00:00`) and reporting days are
--     `YYYY-MM-DD` strings, so text comparison equals chronological comparison;
--   * JSON columns hold compact, key-sorted JSON (`sort_keys=True, separators=(",", ":")`);
--   * no foreign keys on purpose: a metrics row may legitimately arrive before the property it
--     belongs to, and Wave 3 crawls write pages/issues independently of provider sync order.
--
-- The tables, column names and primary keys below are the stable interface. Add a migration
-- instead of renaming anything once user data exists.

-- Schema migrations ledger. Applied by `store.db.migrate()` in version order; every row is
-- append-only so a half-applied upgrade can resume instead of re-running DDL.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER NOT NULL PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);

-- Window aggregates (F14): non-daily provider rows keyed by their real window.
-- `metric_daily` holds only true per-day rows (queries with a `date` dimension);
-- everything else lands here so a weekly aggregate dated at its end can never
-- overwrite (or be overwritten by) a daily row again. Rows written before this
-- table existed stay in `metric_daily` with accuracy='legacy_unknown_granularity':
-- their window is unrecoverable, so daily reads exclude them unless explicitly requested.
CREATE TABLE IF NOT EXISTS metric_windows (
    property_id TEXT NOT NULL,
    source      TEXT NOT NULL,
    start       TEXT NOT NULL,
    "end"       TEXT NOT NULL,
    "scope"     TEXT NOT NULL,
    "key"       TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    accuracy    TEXT,
    PRIMARY KEY (property_id, source, start, "end", "scope", "key", metric)
);

CREATE INDEX IF NOT EXISTS idx_metric_windows_end
    ON metric_windows (property_id, "end");

CREATE TABLE IF NOT EXISTS properties (
    provider      TEXT NOT NULL,
    property_id   TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    host_id       TEXT,
    counter_id    TEXT,
    verified      INTEGER NOT NULL DEFAULT 1,
    config_json   TEXT,
    PRIMARY KEY (provider, property_id)
);

CREATE TABLE IF NOT EXISTS metric_daily (
    property_id TEXT NOT NULL,
    source      TEXT NOT NULL,
    "date"      TEXT NOT NULL,
    "scope"     TEXT NOT NULL,
    "key"       TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    accuracy    TEXT,
    PRIMARY KEY (property_id, source, "date", "scope", "key", metric)
);

CREATE INDEX IF NOT EXISTS idx_metric_daily_date
    ON metric_daily (property_id, "date");

CREATE INDEX IF NOT EXISTS idx_metric_daily_metric
    ON metric_daily (property_id, metric, "date");

CREATE TABLE IF NOT EXISTS pages (
    property_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      INTEGER NOT NULL,
    index_state TEXT,
    canonical   TEXT,
    title_hash  TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (property_id, url)
);

CREATE INDEX IF NOT EXISTS idx_pages_index_state
    ON pages (property_id, index_state);

CREATE INDEX IF NOT EXISTS idx_pages_last_seen
    ON pages (property_id, last_seen);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_url   TEXT,
    started_at  TEXT,
    finished_at TEXT,
    urls        INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    status      TEXT
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_started
    ON crawl_runs (started_at);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     TEXT NOT NULL,
    severity    TEXT NOT NULL,
    property_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    detail_json TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',
    UNIQUE (rule_id, property_id, url)
);

CREATE INDEX IF NOT EXISTS idx_issues_state
    ON issues (property_id, state, severity);

CREATE INDEX IF NOT EXISTS idx_issues_last_seen
    ON issues (property_id, last_seen);

CREATE TABLE IF NOT EXISTS snapshots (
    kind         TEXT NOT NULL,
    property_id  TEXT NOT NULL,
    period       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_gz   BLOB NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (kind, property_id, period)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_property_period
    ON snapshots (property_id, period);

CREATE TABLE IF NOT EXISTS sends (
    kind         TEXT NOT NULL,
    property_id  TEXT NOT NULL,
    period       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    channel      TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    status       TEXT NOT NULL,
    PRIMARY KEY (kind, property_id, period, channel, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_sends_property_period
    ON sends (property_id, period);

CREATE TABLE IF NOT EXISTS bot_daily (
    property_id TEXT NOT NULL,
    "date"      TEXT NOT NULL,
    bot         TEXT NOT NULL,
    status      TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    p95_ms      REAL,
    PRIMARY KEY (property_id, "date", bot, status)
);

CREATE INDEX IF NOT EXISTS idx_bot_daily_date
    ON bot_daily (property_id, "date");

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    property_id TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stats_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_kind_property
    ON runs (kind, property_id, started_at);

-- Finding/task lifecycle (F2/F7): a finding becomes a task that is triaged, fixed and
-- verified. `(kind, property_id, url, rule_id)` is the natural key, so a re-run of a crawl
-- or a decay scan never duplicates a task. `state` is a closed set: the repository validates
-- transitions, and the CHECK keeps raw SQL from inventing a state the CLI cannot explain.
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    property_id  TEXT NOT NULL,
    url          TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'open'
                 CHECK (state IN ('open', 'in_progress', 'verified', 'dismissed')),
    payload_json TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (kind, property_id, url, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_property_state
    ON tasks (property_id, state, updated_at);

CREATE INDEX IF NOT EXISTS idx_tasks_kind_state
    ON tasks (kind, state, updated_at);

-- Provider-reported indexing events (0.7.0): one row per URL, event name and day. The table is
-- append-only in spirit -- an event is a fact a provider reported, so a replay must neither
-- duplicate it nor move its first sighting. `(provider, property_id, url, event, event_date)` is
-- the natural key; `first_seen` never moves and `last_seen` only advances. `detail_json` keeps
-- the provider payload compact so the digest can explain an event without a second API call.
CREATE TABLE IF NOT EXISTS indexing_events (
    provider    TEXT NOT NULL,
    property_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    event       TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    detail_json TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (provider, property_id, url, event, event_date)
);

CREATE INDEX IF NOT EXISTS idx_indexing_events_property_date
    ON indexing_events (property_id, event_date);

CREATE INDEX IF NOT EXISTS idx_indexing_events_property_url
    ON indexing_events (property_id, url, event_date);
