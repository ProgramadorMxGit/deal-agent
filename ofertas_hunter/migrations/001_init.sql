-- ofertas_hunter — esquema inicial.
-- Aplicado por src/ofertas_hunter/db.py:init_db().
-- Documentación: docs/SCHEMA.md
-- Fecha: 2026-05-25

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace          TEXT NOT NULL,
    marketplace_id       TEXT,
    url_canonical        TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL,
    brand                TEXT,
    category             TEXT,
    category_inferred    INTEGER NOT NULL DEFAULT 0,
    condition            TEXT NOT NULL DEFAULT 'new',
    image_url            TEXT,
    affiliate_link       TEXT,
    affiliate_product_id TEXT,
    commission_text      TEXT,
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_marketplace_id ON products(marketplace, marketplace_id);
CREATE INDEX IF NOT EXISTS idx_products_category       ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_brand          ON products(brand);

-- ---------------------------------------------------------------------------
-- price_observations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    current_price    REAL,
    previous_price   REAL,
    currency         TEXT NOT NULL DEFAULT 'MXN',
    discount_percent REAL,
    has_stock        INTEGER,
    source           TEXT NOT NULL,
    raw_signals_json TEXT,
    observed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_obs_product  ON price_observations(product_id);
CREATE INDEX IF NOT EXISTS idx_price_obs_observed ON price_observations(observed_at);

-- ---------------------------------------------------------------------------
-- offers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS offers (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id                    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    current_price_observation_id  INTEGER REFERENCES price_observations(id),
    classification                TEXT NOT NULL,
    score                         INTEGER NOT NULL,
    reasons_json                  TEXT NOT NULL,
    discount_percent              REAL,
    state                         TEXT NOT NULL,
    created_at                    TEXT NOT NULL,
    updated_at                    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_state    ON offers(state);
CREATE INDEX IF NOT EXISTS idx_offers_product  ON offers(product_id);

-- ---------------------------------------------------------------------------
-- outbox
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id             INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    type                 TEXT NOT NULL,
    enqueued_at          TEXT NOT NULL,
    scheduled_for        TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_attempt_at      TEXT,
    state                TEXT NOT NULL DEFAULT 'pending',
    message_payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_state_type ON outbox(state, type);
CREATE INDEX IF NOT EXISTS idx_outbox_enqueued   ON outbox(enqueued_at);

-- ---------------------------------------------------------------------------
-- published_messages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS published_messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id          INTEGER REFERENCES outbox(id) ON DELETE SET NULL,
    offer_id           INTEGER REFERENCES offers(id) ON DELETE SET NULL,
    sent_at            TEXT NOT NULL,
    success            INTEGER NOT NULL,
    evolution_response TEXT,
    message_text       TEXT NOT NULL,
    media_url          TEXT
);
CREATE INDEX IF NOT EXISTS idx_published_sent ON published_messages(sent_at);

-- ---------------------------------------------------------------------------
-- discarded_candidates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS discarded_candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    raw_payload_json TEXT,
    reason           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discarded_reason ON discarded_candidates(reason);

-- ---------------------------------------------------------------------------
-- visited_urls
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visited_urls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace   TEXT NOT NULL,
    url_canonical TEXT NOT NULL,
    visited_at    TEXT NOT NULL,
    UNIQUE(marketplace, url_canonical)
);

-- ---------------------------------------------------------------------------
-- frontier
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS frontier (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace   TEXT NOT NULL,
    url_canonical TEXT NOT NULL,
    url_type      TEXT NOT NULL,
    score         REAL NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL,
    retries       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(marketplace, url_canonical)
);
CREATE INDEX IF NOT EXISTS idx_frontier_marketplace_score ON frontier(marketplace, score DESC);

-- ---------------------------------------------------------------------------
-- dom_snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dom_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    context     TEXT NOT NULL,
    url         TEXT,
    content     TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dom_snapshots_market_ctx ON dom_snapshots(marketplace, context);

-- ---------------------------------------------------------------------------
-- selector_versions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS selector_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace     TEXT NOT NULL,
    context         TEXT NOT NULL,
    key             TEXT NOT NULL,
    selector_value  TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    applied_by      TEXT NOT NULL,
    fixture_path    TEXT,
    test_pass       INTEGER NOT NULL,
    reverted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_selector_versions_lookup
    ON selector_versions(marketplace, context, key, applied_at DESC);

-- ---------------------------------------------------------------------------
-- agent_runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name     TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    status         TEXT NOT NULL,
    summary_json   TEXT,
    last_heartbeat TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name, started_at DESC);

-- ---------------------------------------------------------------------------
-- self_patches
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS self_patches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path    TEXT NOT NULL,
    diff         TEXT NOT NULL,
    applied_at   TEXT NOT NULL,
    tests_passed INTEGER NOT NULL,
    reverted_at  TEXT,
    reason       TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- memory_summaries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_summaries_kind ON memory_summaries(kind, generated_at DESC);

-- ---------------------------------------------------------------------------
-- runtime_events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    payload_json    TEXT,
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runtime_events_severity ON runtime_events(severity, created_at DESC);

-- ---------------------------------------------------------------------------
-- telegram_messages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telegram_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    text         TEXT,
    image_path   TEXT,
    original_url TEXT,
    resolved_url TEXT,
    captured_at  TEXT NOT NULL,
    processed_at TEXT,
    skip_reason  TEXT,
    UNIQUE(channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_msgs_processed ON telegram_messages(processed_at);

-- ---------------------------------------------------------------------------
-- price_error_examples
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_error_examples (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL,
    payload_json            TEXT NOT NULL,
    classification_expected TEXT NOT NULL,
    confidence_expected     TEXT,
    notes                   TEXT
);

-- ---------------------------------------------------------------------------
-- category_price_ranges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category_price_ranges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,
    brand        TEXT,
    min_normal   REAL,
    max_normal   REAL,
    median       REAL,
    p10          REAL,
    p90          REAL,
    sample_size  INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    UNIQUE(category, brand)
);

-- ---------------------------------------------------------------------------
-- product_aliases
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS product_aliases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    source     TEXT NOT NULL,
    UNIQUE(product_id, alias)
);

-- ---------------------------------------------------------------------------
-- resolved_urls
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resolved_urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    original_url TEXT NOT NULL UNIQUE,
    final_url    TEXT,
    http_status  INTEGER,
    resolved_at  TEXT NOT NULL
);
