# Esquema SQLite

> Toda la información persistente vive en `data/ofertas_hunter.db` con
> `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
> `temp_store=MEMORY`, `busy_timeout=5000`.
>
> Migraciones: `migrations/001_init.sql`. Cada cambio futuro abre un nuevo
> archivo numerado (`002_…`).

## Convenciones

- IDs: `INTEGER PRIMARY KEY AUTOINCREMENT` salvo cuando hay clave natural.
- Timestamps: `TEXT NOT NULL` con ISO8601 UTC (`YYYY-MM-DDTHH:MM:SS.sssZ`).
- Booleans: `INTEGER` (0/1).
- JSON: `TEXT` (validado a nivel aplicación).
- `marketplace` es enum textual: `amazon | mercadolibre | telegram | other`.
- `source` (origen del candidato): `amazon_hunter | mercadolibre_hunter | telegram | manual`.

## Tablas

### products

Catálogo de productos vistos al menos una vez. Clave natural opcional
`(marketplace, marketplace_id)` para evitar duplicados cuando se conoce el id
canónico.

```sql
CREATE TABLE products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace       TEXT NOT NULL,
    marketplace_id    TEXT,                    -- ASIN, MLM, etc.
    url_canonical     TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    brand             TEXT,
    category          TEXT,
    category_inferred INTEGER NOT NULL DEFAULT 0,
    condition         TEXT NOT NULL DEFAULT 'new',  -- new | used | refurbished | unknown
    image_url         TEXT,
    affiliate_link    TEXT,
    affiliate_product_id TEXT,
    commission_text   TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);
CREATE INDEX idx_products_marketplace_id ON products(marketplace, marketplace_id);
CREATE INDEX idx_products_category       ON products(category);
CREATE INDEX idx_products_brand          ON products(brand);
```

### price_observations

Histórico de precios. Una fila por observación.

```sql
CREATE TABLE price_observations (
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
CREATE INDEX idx_price_obs_product   ON price_observations(product_id);
CREATE INDEX idx_price_obs_observed  ON price_observations(observed_at);
```

### offers

Cada vez que el `price_intelligence` evalúa un candidato, escribe una fila
con la decisión tomada.

```sql
CREATE TABLE offers (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id                    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    current_price_observation_id  INTEGER REFERENCES price_observations(id),
    classification                TEXT NOT NULL,    -- price_error_confirmed | possible_price_error | suspicious_deal | normal_offer | no_price_error
    score                         INTEGER NOT NULL,
    reasons_json                  TEXT NOT NULL,
    discount_percent              REAL,
    state                         TEXT NOT NULL,    -- candidate | eligible | published | discarded | expired | watchlist
    created_at                    TEXT NOT NULL,
    updated_at                    TEXT NOT NULL
);
CREATE INDEX idx_offers_state    ON offers(state);
CREATE INDEX idx_offers_product  ON offers(product_id);
```

### outbox

Cola persistente de publicación. Un solo dispatcher la consume.

```sql
CREATE TABLE outbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id          INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    type              TEXT NOT NULL,                     -- normal | price_error | possible_pe
    enqueued_at       TEXT NOT NULL,
    scheduled_for     TEXT,                              -- NULL = lo antes posible
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',   -- pending | in_flight | sent | failed | discarded
    message_payload_json TEXT NOT NULL                   -- payload estable: title, price, url, image_url, type, confidence_label
);
CREATE INDEX idx_outbox_state_type ON outbox(state, type);
CREATE INDEX idx_outbox_enqueued   ON outbox(enqueued_at);
```

### published_messages

```sql
CREATE TABLE published_messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id          INTEGER REFERENCES outbox(id) ON DELETE SET NULL,
    offer_id           INTEGER REFERENCES offers(id) ON DELETE SET NULL,
    sent_at            TEXT NOT NULL,
    success            INTEGER NOT NULL,
    evolution_response TEXT,
    message_text       TEXT NOT NULL,
    media_url          TEXT
);
CREATE INDEX idx_published_sent ON published_messages(sent_at);
```

### discarded_candidates

```sql
CREATE TABLE discarded_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,                  -- amazon | mercadolibre | telegram
    raw_payload_json TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_discarded_reason ON discarded_candidates(reason);
```

### visited_urls

```sql
CREATE TABLE visited_urls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace   TEXT NOT NULL,
    url_canonical TEXT NOT NULL,
    visited_at    TEXT NOT NULL,
    UNIQUE(marketplace, url_canonical)
);
```

### frontier

Cola de exploración por marketplace.

```sql
CREATE TABLE frontier (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace   TEXT NOT NULL,
    url_canonical TEXT NOT NULL,
    url_type      TEXT NOT NULL,           -- category | listing | product | unknown
    score         REAL NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL,
    retries       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(marketplace, url_canonical)
);
CREATE INDEX idx_frontier_marketplace_score ON frontier(marketplace, score DESC);
```

### dom_snapshots

```sql
CREATE TABLE dom_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace  TEXT NOT NULL,
    context      TEXT NOT NULL,            -- search_results | product_page | listing | category
    url          TEXT,
    content      TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    reason       TEXT NOT NULL             -- degradation | manual | healed | smoke
);
CREATE INDEX idx_dom_snapshots_market_ctx ON dom_snapshots(marketplace, context);
```

### selector_versions

```sql
CREATE TABLE selector_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace     TEXT NOT NULL,
    context         TEXT NOT NULL,
    key             TEXT NOT NULL,
    selector_value  TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    applied_by      TEXT NOT NULL,         -- heuristic | llm | manual
    fixture_path    TEXT,
    test_pass       INTEGER NOT NULL,
    reverted_at     TEXT
);
CREATE INDEX idx_selector_versions_lookup ON selector_versions(marketplace, context, key, applied_at DESC);
```

### agent_runs

```sql
CREATE TABLE agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL,         -- running | ok | error | killed
    summary_json    TEXT,
    last_heartbeat  TEXT
);
CREATE INDEX idx_agent_runs_agent ON agent_runs(agent_name, started_at DESC);
```

### self_patches

```sql
CREATE TABLE self_patches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL,
    diff          TEXT NOT NULL,
    applied_at    TEXT NOT NULL,
    tests_passed  INTEGER NOT NULL,
    reverted_at   TEXT,
    reason        TEXT NOT NULL
);
```

### memory_summaries

```sql
CREATE TABLE memory_summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,           -- price_patterns | unstable_selectors | discard_reasons | etc.
    content       TEXT NOT NULL,
    generated_at  TEXT NOT NULL
);
CREATE INDEX idx_memory_summaries_kind ON memory_summaries(kind, generated_at DESC);
```

### runtime_events

```sql
CREATE TABLE runtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,        -- cookie_expiry | captcha | dom_degradation | selector_healed | publish_failed | etc.
    severity        TEXT NOT NULL,        -- info | warning | error | critical
    payload_json    TEXT,
    created_at      TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX idx_runtime_events_severity ON runtime_events(severity, created_at DESC);
```

### telegram_messages

```sql
CREATE TABLE telegram_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    message_id    INTEGER NOT NULL,
    text          TEXT,
    image_path    TEXT,
    original_url  TEXT,
    resolved_url  TEXT,
    captured_at   TEXT NOT NULL,
    processed_at  TEXT,
    skip_reason   TEXT,
    UNIQUE(channel, message_id)
);
CREATE INDEX idx_tg_msgs_processed ON telegram_messages(processed_at);
```

### price_error_examples

Fixtures de entrenamiento (los ejemplos A-M de la spec más los que se vayan
añadiendo).

```sql
CREATE TABLE price_error_examples (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source                   TEXT NOT NULL,
    payload_json             TEXT NOT NULL,
    classification_expected  TEXT NOT NULL,
    confidence_expected      TEXT,
    notes                    TEXT
);
```

### category_price_ranges

```sql
CREATE TABLE category_price_ranges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT NOT NULL,
    brand         TEXT,
    min_normal    REAL,
    max_normal    REAL,
    median        REAL,
    p10           REAL,
    p90           REAL,
    sample_size   INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    UNIQUE(category, brand)
);
```

### product_aliases

```sql
CREATE TABLE product_aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    source      TEXT NOT NULL,
    UNIQUE(product_id, alias)
);
```

### resolved_urls

```sql
CREATE TABLE resolved_urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    original_url TEXT NOT NULL UNIQUE,
    final_url    TEXT,
    http_status  INTEGER,
    resolved_at  TEXT NOT NULL
);
```

## Vistas útiles (futuras)

- `v_unstable_selectors`: selectores con > 3 versiones en 30 días.
- `v_recent_anomalies`: ofertas con score > 80 últimas 24h.
- `v_outbox_summary`: agregado por type/state.
