# Plan de migración

> Mapea cada pieza de los proyectos `bot_diversidad_global` y `AmazonScrapperIA`
> al nuevo proyecto `ofertas_hunter`.

## 1. Reglas

1. **No borrar** ningún archivo de los proyectos originales hasta que el nuevo
   los reemplace y pase tests.
2. **Reutilizar literal** cuando el código sea correcto y sólo cambia el path.
3. **Adaptar** cuando el contrato cambia (multi-marketplace, SQLite, etc.).
4. **Reescribir** cuando el código está acoplado o no escala.
5. **Documentar cada decisión** de descarte en `docs/MIGRATION_PLAN.md`
   (este archivo) o `docs/audit/<proyecto>.md`.

## 2. Mapeo `bot_diversidad_global` → `ofertas_hunter`

| Origen | Destino | Acción |
|---|---|---|
| `src/main.py` | `src/ofertas_hunter/__main__.py` | ADAPTAR (CLI multi-modo) |
| `src/orchestrator.py` | `src/ofertas_hunter/orchestrator.py` | REESCRIBIR (multi-agente real) |
| `src/browser_worker.py` (extract_affiliate_link) | `src/ofertas_hunter/marketplaces/mercadolibre/hunter.py` | ADAPTAR |
| `src/session_loader.py` | `src/ofertas_hunter/marketplaces/mercadolibre/session.py` | REUTILIZAR |
| `src/models.py` | `src/ofertas_hunter/models.py` (ampliado) | ADAPTAR |
| `src/utils.py` (canonicalize, blocked) | `src/ofertas_hunter/marketplaces/mercadolibre/url_utils.py` | REUTILIZAR |
| `src/config.py` | `src/ofertas_hunter/config.py` | ADAPTAR (pydantic + env) |
| `src/cookie_renewal_notifier.py` | `src/ofertas_hunter/agents/runtime_watchdog.py` (parcial) | REESCRIBIR |
| `src/explorers/*` | `src/ofertas_hunter/marketplaces/mercadolibre/explorers/*` | REUTILIZAR |
| `src/extraction/price_parser.py` | `src/ofertas_hunter/marketplaces/mercadolibre/price_parser.py` | REUTILIZAR |
| `src/extraction/product_parser.py` | `src/ofertas_hunter/marketplaces/mercadolibre/product_parser.py` | REUTILIZAR |
| `src/evaluation/offer_evaluator.py` (apply_hard_rule, parse_kiro_response) | `src/ofertas_hunter/intelligence/llm_evaluator.py` (sin device-flow) | ADAPTAR |
| `src/storage/json_store.py` | reemplazado por SQLite (`db.py` + `repositories/`) | REESCRIBIR |
| `src/storage/state_store.py` | reemplazado por tablas `frontier`, `visited_urls`, `agent_runs` | REESCRIBIR |
| `tests/conftest.py` | `tests/conftest.py` | REUTILIZAR |
| `tests/unit/test_*.py` (9 archivos) | `tests/unit/marketplaces/mercadolibre/*.py` | REUTILIZAR (ajustando imports) |
| `tests/integration/test_smoke_real_page.py` | `tests/integration/marketplaces/mercadolibre/test_smoke.py` | ADAPTAR |
| `scripts/check_cookies.py` | `scripts/check_cookies.py` | ADAPTAR |
| `scripts/capture_snapshot.py` | `scripts/capture_snapshot.py` | REUTILIZAR |
| `scripts/test_affiliate_link.py` | `scripts/test_affiliate_link.py` | REUTILIZAR |
| `scripts/enrich_affiliate_links.py` | n/a | DESCARTAR |
| `scripts/debug_modal.py`, `debug_price.py` | n/a | DESCARTAR (snapshots ya capturados) |
| `scripts/vps_*.sh`, `check_*.sh`, `monitor_loop.sh` | n/a | DESCARTAR (cross-repo) |
| `scripts/debug_*_dump.html` | n/a | DESCARTAR (versionado por error, dejar en `_legacy/`) |
| `deploy/systemd/bot-diversidad-global.service` | `deploy/systemd/ofertas-hunter.service` | ADAPTAR |
| `config/settings.example.json` | `config/settings.example.yaml` | ADAPTAR |
| `config/seeds.example.json` | `config/seeds/mercadolibre.json` | ADAPTAR |
| `connect-vps.ps1` | n/a | DESCARTAR (mover a env vars) |
| `MEMORY.md` | n/a | NO copiar (contiene secretos VPS) |
| `requirements.txt` | base de `requirements.txt` nuevo | REUTILIZAR |
| `pytest.ini` | `pytest.ini` | REUTILIZAR |

## 3. Mapeo `AmazonScrapperIA` → `ofertas_hunter`

| Origen | Destino | Acción |
|---|---|---|
| `scraper.py`, `run_with_kiro.py` | n/a | DESCARTAR |
| `mcp_server.py` | `src/ofertas_hunter/agents/mcp_server.py` (futuro) | ADAPTAR |
| `telegram_sender.py` | n/a | DESCARTAR (publicación va a WhatsApp) |
| `send_pending.py` | n/a | DESCARTAR |
| `setup_kiro_login.py` | `scripts/kiro_login.py` | ADAPTAR |
| `test_*.py` | n/a (rehacer en pytest) | DESCARTAR |
| `src/browser_worker.py` (stealth, captcha, antibot) | `src/ofertas_hunter/marketplaces/amazon/hunter.py` y `marketplaces/_browser/stealth.py` | ADAPTAR |
| `src/price_parser.py` (Amazon) | `src/ofertas_hunter/marketplaces/amazon/price_parser.py` | REUTILIZAR |
| `src/dom_healer.py` | `src/ofertas_hunter/self_healing/dom_healer.py` | ADAPTAR |
| `src/memory_store.py` | reemplazado por SQLite (`memory/store.py`) | REESCRIBIR |
| `src/ai_evaluator.py` | `src/ofertas_hunter/intelligence/llm_evaluator.py` | ADAPTAR |
| `config/settings.json` | `config/settings.example.yaml` (merge) | ADAPTAR |
| `config/selectors.json` | `config/selectors/amazon.json` | REUTILIZAR |
| `config/seeds.json` | `config/seeds/amazon.json` | REUTILIZAR |
| `data/heal_samples/sample_*.html` (97) | `tests/fixtures/snapshots/amazon/` (selección 5-10) | REUTILIZAR (selectivo) |
| `data/offers.json`, `state.json`, `memory.json` | n/a | DESCARTAR (datos legacy) |
| `MEMORY.md` | n/a | NO copiar |
| `prompt.txt`, `run_bot.ps1` | n/a | DESCARTAR |

## 4. Estructura final

```
ofertas_hunter/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MIGRATION_PLAN.md          ← este archivo
│   ├── AGENTS.md
│   ├── RULES.md
│   ├── PRICE_ERROR_DETECTION.md
│   ├── TELEGRAM_PRICE_ERROR_PATTERNS.md
│   ├── DEPLOY.md
│   ├── SCHEMA.md
│   └── audit/
│       ├── AmazonScrapperIA.md
│       └── bot_diversidad_global.md
│
├── src/ofertas_hunter/
│   ├── __init__.py
│   ├── __main__.py                ← CLI: hunt | listen | dispatch | revalidate | export
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── orchestrator.py
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py                ← BaseAgent abstracto
│   │   ├── supervisor.py
│   │   ├── amazon_hunter.py
│   │   ├── mercadolibre_hunter.py
│   │   ├── telegram_listener.py
│   │   ├── price_intelligence.py
│   │   ├── outbox_dispatcher.py
│   │   ├── memory_compressor.py
│   │   ├── self_repair_agent.py
│   │   └── runtime_watchdog.py
│   │
│   ├── marketplaces/
│   │   ├── __init__.py
│   │   ├── _browser/
│   │   │   ├── __init__.py
│   │   │   ├── manager.py         ← Playwright lifecycle
│   │   │   └── stealth.py         ← STEALTH_SCRIPT, UAS, viewports
│   │   ├── amazon/
│   │   │   ├── __init__.py
│   │   │   ├── hunter.py
│   │   │   ├── price_parser.py
│   │   │   ├── product_parser.py
│   │   │   └── url_utils.py
│   │   └── mercadolibre/
│   │       ├── __init__.py
│   │       ├── hunter.py
│   │       ├── price_parser.py
│   │       ├── product_parser.py
│   │       ├── session.py         ← cookie loader
│   │       ├── url_utils.py
│   │       └── explorers/
│   │           ├── category.py
│   │           ├── listing.py
│   │           └── product.py
│   │
│   ├── extraction/
│   │   ├── __init__.py
│   │   ├── image_resolver.py
│   │   ├── price_utils.py         ← parse_price_text, monthly detection
│   │   └── url_resolver.py        ← shortlinks
│   │
│   ├── intelligence/
│   │   ├── __init__.py
│   │   ├── discount_calculator.py
│   │   ├── price_error_scorer.py
│   │   ├── category_inferer.py
│   │   ├── llm_evaluator.py
│   │   ├── historical_anomaly.py
│   │   └── category_ranges.py
│   │
│   ├── publishing/
│   │   ├── __init__.py
│   │   ├── formatter.py
│   │   ├── whatsapp_evolution.py
│   │   └── image_downloader.py
│   │
│   ├── dispatching/
│   │   ├── __init__.py
│   │   ├── outbox.py
│   │   ├── dispatcher.py
│   │   └── cooldown.py
│   │
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── listener.py
│   │   ├── message_parser.py
│   │   └── signal_extractor.py
│   │
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── store.py
│   │   ├── compressor.py
│   │   └── summary_writer.py
│   │
│   ├── self_healing/
│   │   ├── __init__.py
│   │   ├── dom_healer.py
│   │   ├── degradation_monitor.py
│   │   ├── selector_versioner.py
│   │   └── self_repair.py
│   │
│   └── repositories/
│       ├── __init__.py
│       ├── products.py
│       ├── price_observations.py
│       ├── offers.py
│       ├── outbox.py
│       ├── runtime_events.py
│       ├── selector_versions.py
│       └── memory.py
│
├── migrations/
│   └── 001_init.sql
│
├── config/
│   ├── settings.example.yaml
│   ├── seeds/
│   │   ├── amazon.json
│   │   └── mercadolibre.json
│   ├── selectors/
│   │   ├── amazon.json
│   │   └── mercadolibre.json
│   └── category_ranges.json
│
├── secrets/
│   ├── .gitkeep
│   ├── mercadolibre_cookies.example.json
│   └── README.md                  ← cómo obtener cookies
│
├── data/                          ← gitignored
│   ├── ofertas_hunter.db          ← SQLite WAL
│   ├── ofertas_hunter.db-wal
│   ├── ofertas_hunter.db-shm
│   ├── exports/                   ← JSON exports
│   ├── heal_samples/              ← snapshots de healing
│   └── images/                    ← images cache
│
├── logs/
│   └── .gitkeep
│
├── scripts/
│   ├── status.sh                  ← VPS
│   ├── run_local.ps1
│   ├── run_local.sh
│   ├── run_worker.sh
│   ├── run_dispatcher.sh
│   ├── check_db.py
│   ├── test_whatsapp.py
│   ├── test_telegram.py
│   ├── test_playwright.py
│   ├── check_cookies.py
│   ├── revalidate_outbox.py
│   ├── export_memory_summary.py
│   ├── capture_snapshot.py
│   ├── test_affiliate_link.py
│   └── kiro_login.py
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── snapshots/
│   │   │   ├── amazon/
│   │   │   └── mercadolibre/
│   │   ├── price_errors/          ← ejemplos A-M de la spec
│   │   └── synthetic/
│   ├── unit/
│   │   ├── core/
│   │   ├── intelligence/
│   │   │   ├── test_price_error_scorer.py    ← spec mandatory
│   │   │   └── test_discount_calculator.py
│   │   ├── publishing/
│   │   │   └── test_formatter.py
│   │   ├── dispatching/
│   │   │   ├── test_outbox.py
│   │   │   ├── test_cooldown.py
│   │   │   └── test_revalidation.py
│   │   ├── telegram/
│   │   │   ├── test_message_parser.py
│   │   │   └── test_signal_extractor.py
│   │   ├── marketplaces/
│   │   │   ├── amazon/
│   │   │   │   ├── test_price_parser.py
│   │   │   │   └── test_product_parser.py
│   │   │   └── mercadolibre/
│   │   │       ├── test_price_parser.py
│   │   │       ├── test_product_parser.py
│   │   │       ├── test_session_loader.py
│   │   │       └── test_url_blocklist.py
│   │   └── self_healing/
│   │       └── test_dom_healer.py
│   └── integration/
│       └── test_end_to_end_flow.py
│
├── deploy/
│   ├── systemd/
│   │   ├── ofertas-hunter.service       ← orchestrator
│   │   ├── ofertas-hunter-dispatcher.service
│   │   └── ofertas-hunter-telegram.service
│   └── docker/
│       └── Dockerfile
│
├── training/
│   └── price_errors/              ← duplica fixtures para entrenamiento
│
├── pyproject.toml
├── requirements.txt
├── pytest.ini
├── .env.example
├── .gitignore
└── README.md
```

## 5. Fases

### Fase 1 — Auditoría y diseño (este commit)

- [x] Auditar `bot_diversidad_global` → `docs/audit/bot_diversidad_global.md`
- [x] Auditar `AmazonScrapperIA` → `docs/audit/AmazonScrapperIA.md`
- [x] Producir `docs/ARCHITECTURE.md`
- [x] Producir `docs/MIGRATION_PLAN.md` (este archivo)
- [x] Producir `docs/AGENTS.md`
- [x] Producir `docs/RULES.md`
- [x] Producir `docs/PRICE_ERROR_DETECTION.md`
- [x] Producir `docs/TELEGRAM_PRICE_ERROR_PATTERNS.md`
- [x] Esquema SQLite inicial (`migrations/001_init.sql`)
- [x] Tests básicos (price_error_scorer, formatter, cooldown, outbox)
- [x] Fixtures de errores de precio (ejemplos A-M)

### Fase 2 — Core limpio

- [ ] `core/config.py`, `db.py`, `models.py`, `logging_setup.py`
- [ ] `repositories/*` con SQLite
- [ ] `intelligence/price_error_scorer.py`, `discount_calculator.py`
- [ ] `publishing/formatter.py`, `whatsapp_evolution.py`
- [ ] `dispatching/outbox.py`, `cooldown.py`, `dispatcher.py`
- [ ] Telemetría: `runtime_events`, `agent_runs`

### Fase 3 — Migración y multi-fuente

- [ ] Migrar lógica útil de `AmazonScrapperIA` (browser, price_parser, dom_healer)
- [ ] Migrar lógica útil de `bot_diversidad_global` (cookies, ML extractors,
  affiliate modal, explorers, blocklist)
- [ ] Implementar `agents/amazon_hunter.py`, `mercadolibre_hunter.py`
- [ ] Implementar `telegram/listener.py` con Telethon
- [ ] Tests de fixtures e2e

### Fase 4 — Operación y resiliencia

- [ ] `self_healing/dom_healer.py` con tests automáticos antes de patch
- [ ] `agents/runtime_watchdog.py`
- [ ] `agents/memory_compressor.py`
- [ ] Scripts de mantenimiento
- [ ] Servicio systemd y Docker
- [ ] Deploy doc

## 6. Estado actual de Fase 1

Ver el directorio `ofertas_hunter/` después del commit para los artefactos
producidos.
