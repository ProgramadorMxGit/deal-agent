# changes.md — `ofertas_hunter`

Log de decisiones por bloque, alineado con la spec del usuario.

---

## 2026-05-25 — Fase 1 completa + Fase 2 mínima

### Auditorías

- Auditoría completa de `bot_diversidad_global` → [`docs/audit/bot_diversidad_global.md`](docs/audit/bot_diversidad_global.md). Veredicto: estructura modular sólida; orchestrator y persistencia JSON deben rediseñarse; `session_loader`, parsers y explorers se reutilizan literal.
- Auditoría completa de `AmazonScrapperIA` → [`docs/audit/AmazonScrapperIA.md`](docs/audit/AmazonScrapperIA.md). Veredicto: `price_parser`, `browser_worker` (stealth) y `dom_healer` son el aporte clave; `MemoryStore` se migra a SQLite; orquestador se descarta.

### Documentación arquitectónica

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — capas, agentes, diagrama, tablas SQLite.
- [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) — mapeo file-by-file de cada legacy al nuevo proyecto.
- [`docs/AGENTS.md`](docs/AGENTS.md) — responsabilidades de los 11 agentes.
- [`docs/RULES.md`](docs/RULES.md) — reglas duras de negocio. **Incluye el formato corregido de oferta normal (JBL Tune 510BT)** indicado por el usuario.
- [`docs/PRICE_ERROR_DETECTION.md`](docs/PRICE_ERROR_DETECTION.md) — `PriceErrorScorer` completo, rangos por categoría, modelo `PriceErrorSignal`.
- [`docs/TELEGRAM_PRICE_ERROR_PATTERNS.md`](docs/TELEGRAM_PRICE_ERROR_PATTERNS.md) — términos de urgencia, emojis, marketplaces.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) — schema SQLite con todas las tablas.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — Windows local + Ubuntu VPS + Docker Compose.

### Esqueleto de código

- `migrations/001_init.sql` — schema completo SQLite con WAL.
- `src/ofertas_hunter/__init__.py`, `__main__.py` (CLI: `init-db`, `check-config`, `run`).
- `src/ofertas_hunter/config.py` — pydantic-settings con `.env`, compatibilidad con envvars del legacy.
- `src/ofertas_hunter/db.py` — conexión SQLite WAL + `init_db` idempotente.
- `src/ofertas_hunter/logging_setup.py`.
- `src/ofertas_hunter/models.py` — dataclasses + enums (`PriceErrorSignal`, `Offer`, `OutboxItem`, `Classification`, etc.).
- `src/ofertas_hunter/intelligence/`
  - `category_ranges.py` — rangos heurísticos iniciales por categoría.
  - `discount_calculator.py` — utilidades de descuento.
  - `price_error_scorer.py` — scorer determinista 0-100 con todas las señales de la spec §2.3-§2.5.
- `src/ofertas_hunter/publishing/formatter.py` — templates `oferta_normal` (formato JBL) y `error_de_precio`.
- `src/ofertas_hunter/dispatching/`
  - `cooldown.py` — `CooldownPolicy` con bypass para errores de precio.
  - `outbox.py` — `InMemoryOutbox` y `SqliteOutbox` con `pick_random_eligible`, `needs_revalidation` (>1h), prioridad de errores de precio.
- `src/ofertas_hunter/telegram/`
  - `signal_extractor.py` — detecta términos "ERROR DE PRECIO", "CORRAN", "DEJA PEDIR", "A SOLO", emojis 🚨🔥‼️.
  - `message_parser.py` — `parse_message` que extrae tienda, marca, categoría, precio, link; **descarta automáticamente links de Mercado Libre** (`skip_reason="mercadolibre_link"`).

### Fixtures y tests

- `tests/fixtures/price_errors/example_a.json` ... `example_m.json` (13 ejemplos de la spec §2.1).
- `tests/conftest.py` con fixtures `price_error_fixtures_dir` y `load_price_error_example`.
- `tests/unit/intelligence/test_price_error_scorer.py` — 25 tests, incluye los **5 tests obligatorios** del scorer + paramétricos sobre A-M.
- `tests/unit/intelligence/test_discount_calculator.py` — 9 tests.
- `tests/unit/publishing/test_formatter.py` — 6 tests, **valida el formato exacto JBL Tune 510BT**.
- `tests/unit/dispatching/test_cooldown_outbox.py` — 9 tests, incluye los obligatorios `test_normal_offer_respects_5_min_cooldown`, `test_price_error_bypasses_cooldown`, `test_outbox_revalidates_after_one_hour`.
- `tests/unit/telegram/test_message_parser.py` — 7 tests, incluye `test_telegram_mercadolibre_links_are_ignored` y `test_telegram_urgency_terms_increase_score`.
- `tests/unit/test_db_init.py` — 3 tests del schema (todas las tablas, idempotencia, WAL).

**Resultado:** `pytest -q` → **59 passed**.

Cobertura de los 16 tests obligatorios de la spec §16:

| Test obligatorio | Estado |
|---|---|
| `test_price_error_laptop_extreme_low_price` | ✅ |
| `test_price_error_iphone_extreme_low_price` | ✅ |
| `test_price_error_samsung_flagship_extreme_low_price` | ✅ |
| `test_price_error_airpods_low_price` | ✅ |
| `test_price_error_visible_91_percent_discount` | ✅ |
| `test_telegram_urgency_terms_increase_score` | ✅ (scorer + extractor) |
| `test_no_image_not_publishable` | ✅ |
| `test_no_price_not_publishable` | ✅ |
| `test_used_product_requires_extreme_discount` | ✅ |
| `test_outbox_revalidates_after_one_hour` | ✅ |
| `test_normal_offer_respects_5_min_cooldown` | ✅ |
| `test_price_error_bypasses_cooldown` | ✅ |
| `test_telegram_mercadolibre_links_are_ignored` | ✅ |
| `test_monthly_payment_not_misread_as_total_price` | ✅ |
| `test_variant_mismatch_reduces_confidence` | ✅ |
| `test_accessory_price_not_confused_with_main_product` | ✅ |

### Decisiones técnicas

- **Python 3.11+**, alineado con el legacy.
- **SQLite con WAL** activado (no aiosqlite todavía; la concurrencia real entra en Fase 3 con un pool si hace falta).
- **pydantic-settings 2.x** para config con `.env`. Compatibilidad con envvars del legacy: `BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH` y `OFERTAS_MELI_BROWSER_COOKIES_PATH` siguen siendo respetados.
- **PriceErrorScorer determinista** (sin LLM). Los thresholds son configurables en `.env`. La spec impone subscores muy específicos; los implemento literalmente y los acoto a [0, 100].
- **Outbox** con prioridad explícita: errores de precio se eligen **antes** de aleatorizar. Las ofertas normales sí se eligen aleatoriamente entre todas las elegibles tras cooldown.
- **Telegram parser** con regla dura: cualquier link `meli.la` o `mercadolibre.com.mx` produce `skip_reason="mercadolibre_link"` y el listener (Fase 3) lo descartará sin enviar a `price_intelligence`.
- **Formato JBL Tune 510BT** validado letra por letra contra el ejemplo del usuario en `test_jbl_example_matches_user_specified_format`.
- **No se borró nada** de los proyectos legacy. Todo el reuso es por copy & adapt en el nuevo paquete.

### Criterios de aceptación cumplidos (spec §14)

- ✅ `pytest` pasa (59/59).
- ✅ El bot puede arrancar sin credenciales: `python -m ofertas_hunter check-config` y `init-db` funcionan en ambiente vacío.
- ✅ Esquema SQLite WAL operativo y todas las 19 tablas exigidas creadas.
- ✅ `Cooldown 5min` para normales, bypass para PE, revalidación >1h: implementado y testeado.
- ✅ `image obligatoria`: `formatter` levanta `FormatterError` si falta; `scorer` marca `not_publishable` con razón `no_image`.
- ✅ Telegram ignora links de Mercado Libre: testeado.
- ✅ Telegram detecta lenguaje de urgencia + emojis: testeado.
- ✅ Documentación clara para correr en VPS Ubuntu en `docs/DEPLOY.md`.
- ✅ Ejemplos extremos (laptop $305, iPhone 16 Pro Max $3,899, Galaxy S24 $1,399, A32 $197) clasificados como `price_error_confirmed` con `very high` o `high`.
- ✅ Producto sin imagen / sin precio / con mensualidad no publicable.

### Pendiente — Fase 3

- `src/ofertas_hunter/marketplaces/amazon/hunter.py` (adaptar `AmazonScrapperIA/src/browser_worker.py`).
- `src/ofertas_hunter/marketplaces/mercadolibre/` (adaptar `bot_diversidad_global/src/explorers/*` y `extraction/*`).
- `src/ofertas_hunter/extraction/url_resolver.py` y `image_resolver.py`.
- `src/ofertas_hunter/dispatching/dispatcher.py` (loop async serializado vs Evolution API).
- `src/ofertas_hunter/publishing/whatsapp_evolution.py`.
- `src/ofertas_hunter/telegram/listener.py` (Telethon).
- `src/ofertas_hunter/agents/*` (clases `BaseAgent` + concretos).
- Migrar tests útiles del legacy (`tests/unit/test_session_loader.py`, `test_price_parser.py`, etc.) ajustando imports.

### Pendiente — Fase 4

- `src/ofertas_hunter/self_healing/dom_healer.py` (adaptar `AmazonScrapperIA/src/dom_healer.py` con tests automáticos antes de patch).
- `src/ofertas_hunter/agents/runtime_watchdog.py`.
- `src/ofertas_hunter/agents/memory_compressor.py`.
- `deploy/systemd/*.service`, `deploy/docker/Dockerfile`.
- Scripts: `scripts/status.sh`, `scripts/run_local.ps1`, `scripts/check_db.py`, `scripts/test_*`, `scripts/revalidate_outbox.py`, `scripts/export_memory_summary.py`.


---

## 2026-05-25 — Fase 3.1 (outbox_dispatcher + Evolution API) ✅

### Archivos creados

- `src/ofertas_hunter/publishing/evolution_client.py` — cliente async httpx para `POST /message/sendText/{instance}` y `POST /message/sendMedia/{instance}`. Acepta `bytes` / path local / data URL / URL http(s) / base64 puro como media. Headers `apikey: {EVOLUTION_API_KEY}`. Modo dry-run que no toca la red. Mock-friendly via `httpx.AsyncClient` inyectable.
- `src/ofertas_hunter/publishing/whatsapp_publisher.py` — `WhatsAppPublisher` que orquesta formatter (JBL / error de precio) + evolution_client. Aplica los gates duros (image_url, current_price, url) antes de formatear. Respeta `enabled=False` retornando `skipped`.
- `src/ofertas_hunter/dispatching/dispatcher.py` — `OutboxDispatcher` serializado con `asyncio.Lock` (un solo publish a la vez), `tick()` para ciclo único y `run_forever()`. Aplica la política de cooldown vía `CooldownPolicy`. Llama a `Revalidator` para items con `enqueued_at > 1h`. `NullRevalidator` por defecto (Fase 3.3/3.4 traerá los Playwright reales). `make_sqlite_published_recorder(conn)` para persistir en `published_messages`.
- `src/ofertas_hunter/__main__.py` — agregados `dispatch [--once]` y `enqueue-sample [--kind normal|price_error]`. `dispatch` arma todo el grafo SQLite + outbox + publisher con la config del `.env`.
- `scripts/test_whatsapp.py` — prueba manual con `--dry-run` (default) o `--send`. Valida config y permite enviar texto o media a un grupo o teléfono.
- `scripts/run_dispatcher.sh` — wrapper bash para VPS.
- `scripts/run_local.ps1` — wrapper PowerShell para Windows con flag `-Once` y `-Sample`.

### Archivos modificados

- `src/ofertas_hunter/config.py`: `publishing_enabled`, `publishing_dry_run`, `dispatcher_idle_sleep_seconds`, `ofertas_whatsapp_group_id` (alias requerido por la spec) + helper `whatsapp_group`.
- `.env.example`: nuevas variables. **PUBLISHING_ENABLED=false** y **PUBLISHING_DRY_RUN=true** por defecto.
- `src/ofertas_hunter/logging_setup.py`: forzar UTF-8 en stdout/stderr (Windows cp1252 fallaba con los emojis 🔥🚨❌✅⚡).
- `src/ofertas_hunter/dispatching/outbox.py`: ningún cambio funcional; ya tenía `pick_random_eligible` con prioridad PE y `needs_revalidation`.

### Tests añadidos (Fase 3.1) — 23 tests nuevos

`tests/unit/publishing/test_evolution_client.py` (8):
- `test_evolution_client_send_text_dry_run` ✅
- `test_evolution_client_send_media_dry_run` ✅
- `test_evolution_client_send_media_dry_run_accepts_https_url` ✅
- `test_evolution_client_send_text_real_uses_post` (mock httpx → 200) ✅
- `test_evolution_client_send_text_real_records_failure` (mock 401) ✅
- `test_evolution_client_real_unconfigured_raises` ✅
- `test_evolution_client_send_media_with_bytes` ✅
- `test_evolution_client_send_media_with_data_url` ✅

`tests/unit/publishing/test_whatsapp_publisher.py` (6):
- `test_publishes_normal_offer_in_dry_run` (valida formato JBL) ✅
- `test_publishes_price_error_in_dry_run` ✅
- `test_publisher_disabled_returns_skipped` ✅
- `test_publish_without_image_fails` ✅
- `test_publish_without_price_fails` ✅
- `test_publish_without_target_group_fails` ✅

`tests/unit/dispatching/test_dispatcher.py` (9):
- `test_dispatcher_does_not_publish_without_image` ✅
- `test_dispatcher_does_not_publish_without_validated_price` ✅
- `test_dispatcher_normal_offer_respects_cooldown` ✅
- `test_dispatcher_price_error_bypasses_cooldown` ✅
- `test_dispatcher_revalidates_offer_older_than_one_hour` ✅
- `test_dispatcher_discards_expired_after_revalidation` ✅
- `test_dispatcher_records_evolution_api_failure` (mock httpx 500) ✅
- `test_dispatcher_random_selects_among_eligible_normal_offers` ✅
- `test_dispatcher_publishing_disabled_keeps_item_pending` ✅

### Ejecución de tests

```
pytest -q
82 passed in 1.06s
```

### Validación end-to-end manual (dry-run)

```powershell
python -m ofertas_hunter init-db
python -m ofertas_hunter enqueue-sample --kind normal
python -m ofertas_hunter enqueue-sample --kind price_error

$env:PUBLISHING_ENABLED = "true"
$env:PUBLISHING_DRY_RUN = "true"
$env:OFERTAS_WHATSAPP_GROUP_ID = "120363426569734715@g.us"
$env:EVOLUTION_BASE_URL = "http://x"
$env:EVOLUTION_API_KEY = "k"
$env:EVOLUTION_INSTANCE = "i"
python -m ofertas_hunter dispatch --once
```

Resultado:
- 3 items encolados en `outbox`.
- Dispatcher prioriza el `price_error` y lo "publica" en dry-run.
- Sin tocar la red.
- `published_messages` registra el envío con `success=1`.
- `outbox.state='sent'`, `attempts=1`.

### Cómo correr el dispatcher

```bash
# Windows / PowerShell
.\scripts\run_local.ps1 -Once
.\scripts\run_local.ps1                       # loop continuo

# Ubuntu VPS (después de Fase 4 systemd)
./scripts/run_dispatcher.sh --once
./scripts/run_dispatcher.sh                   # loop continuo
```

### Cómo activar envío real (cuando el operador lo autorice)

1. Editar `.env`:
   ```
   EVOLUTION_BASE_URL=http://162.251.147.177:8080
   EVOLUTION_API_KEY=dev-evolution-api-key
   EVOLUTION_INSTANCE=mi-instancia
   OFERTAS_WHATSAPP_GROUP_ID=120363426569734715@g.us
   PUBLISHING_ENABLED=true
   PUBLISHING_DRY_RUN=false
   ```
2. Validar con un envío de prueba a tu propio teléfono (no al grupo):
   ```powershell
   python scripts/test_whatsapp.py --send --to 5218338498692 --text "ofertas_hunter probe"
   ```
3. Si el `success=True` y el mensaje llegó, recién ahí encolá una oferta real con `enqueue-sample` o esperá a Fase 3.2/3.3 con los hunters reales.

### Decisiones técnicas

- **httpx async** (no `urllib.request` síncrono como el legacy): permite test con `MockTransport`, timeouts limpios y reuso de conexión cuando se inyecta `client`.
- **Dry-run en el cliente**, no en el publisher. Así el publisher siempre llama a `send_media` y la decisión de tocar la red se hace una sola vez en el cliente. El test `test_evolution_client_send_text_real_uses_post` valida que la URL exacta es `{base}/message/sendText/{instance}` con `apikey` correcto.
- **Toda publicación va por `sendMedia`** porque la spec exige imagen obligatoria. El publisher pasa la imagen como URL pública (Amazon `m.media-amazon.com/...`) y Evolution se encarga de la descarga; si más adelante hace falta forzar base64 (algunos proveedores no aceptan URLs externas), basta con descargar la imagen primero en `image_resolver.py` (Fase 3.3) y pasar bytes.
- **Cooldown sólo aplica a normales** y se respeta tanto en dry-run como en real (test_dispatcher_normal_offer_respects_cooldown lo confirma con un clock inyectable).
- **Bypass para PE**: `pick_random_eligible` ya prioriza errores de precio antes de aleatorizar, validado con un test que ejecuta 30 picks y todos eligen el PE.
- **Revalidación**: el dispatcher llama al `Revalidator` *antes* de pickear, no después. Si la revalidación devuelve `still_eligible=False`, el item queda `discarded` con razón y nunca llega al publisher (test_dispatcher_discards_expired_after_revalidation).
- **`NullRevalidator`** es aceptable en Fase 3.1: el dispatcher tiene gates duros (imagen, precio, url) en el publisher, así que aunque el revalidator no haga nada útil, no se publican datos rotos. Cuando se enchufen los hunters Playwright (Fase 3.3/3.4), un `PlaywrightRevalidator` reemplazará al null y validará página real.

### Pendiente — Fase 3.2 (telegram_listener Telethon)

A iniciar después de tu visto bueno.


---

## 2026-05-25 — Fase 3.2 (telegram_listener Telethon) ✅

### Archivos creados

- `src/ofertas_hunter/telegram/channel_config.py` — `parse_channels(value)` y `ChannelEntry` que clasifica username / título / id.
- `src/ofertas_hunter/telegram/link_resolver.py` — `LinkResolver` async (httpx) con cache SQLite (`resolved_urls`). HEAD → GET fallback, hasta 5 redirects, timeout 6s. `is_shortlink()` e `is_mercadolibre_url()` con comparación de host exacto.
- `src/ofertas_hunter/telegram/candidate_builder.py` — `TelegramCandidateBuilder` que aplica las reglas de la spec §4 y §6 y produce `TelegramCandidate` con clasificación interna (`telegram_price_error_signal | telegram_deal_signal | ignored_mercadolibre_from_telegram | noise`). Si actionable, construye un `OutboxItem` con `requires_live_validation=True`.
- `src/ofertas_hunter/telegram/telethon_listener.py` — `TelethonAdapter` real. Importa Telethon sólo al instanciar; falla con `TelethonImportError` si no está instalado. Resuelve canales por username, título exacto o id numérico.
- `src/ofertas_hunter/telegram/telethon_listener_helpers.py` — helpers para descargar imagen del mensaje y convertirlo a `IncomingMessage` neutral.
- `src/ofertas_hunter/agents/__init__.py` y `src/ofertas_hunter/agents/telegram_listener_agent.py` — `TelegramListenerAgent` con interfaz `TelegramAdapter` (Protocol) para que los tests usen un fake. Maneja dedupe por `(channel, message_id)`, persiste `telegram_messages`, `discarded_candidates` y `outbox` (todo en transacción SQLite).
- `scripts/test_telegram.py` — modo `parse | backfill | listen | check-config`.
- `scripts/run_telegram_listener.sh` — wrapper Linux/VPS.
- `scripts/run_telegram_listener.ps1` — wrapper Windows con flags `-Once`, `-Backfill`, `-Limit`.

### Archivos modificados

- `src/ofertas_hunter/telegram/message_parser.py`:
  - `ParsedTelegramMessage` ahora trae `original_urls`, `hashtags`, `chat_id`, `has_image`.
  - Categoría `smartphone_flagship` se detecta antes que `smartphone` genérica (era un bug que hacía caer iPhone 16 Pro Max en `smartphone`).
  - `_extract_all_urls()` con dedup en orden.
  - `_extract_hashtags()` para `#tags`.
- `src/ofertas_hunter/config.py`:
  - Vars Telegram completas: `telegram_target_channels`, `ofertas_telegram_api_id/hash` (compat), `telegram_backfill_process_budget_per_channel`, `telegram_channel_workers`, `telegram_ignore_mercadolibre_links`, `telegram_link_resolver_timeout_seconds`, `telegram_link_resolver_max_redirects`.
  - Helpers `resolved_telegram_api_id`, `resolved_telegram_api_hash`, `telegram_session_path_resolved`.
- `.env.example` — variables nuevas Telegram + alias legacy.
- `src/ofertas_hunter/__main__.py` — comandos `telegram-check-config`, `telegram-parse-sample`, `telegram-listen [--once] [--limit N]`, `telegram-backfill [--limit N]`.

### Fixtures de mensajes (14)

Bajo `tests/fixtures/telegram/`:
- `laptop_hp_elitebook_walmart_2349.txt`, `asus_vivobook_sams_305.txt`, `ipad_coppel_2611.txt`, `coofandy_amazon_91_percent.txt`, `dell_pro_16_1544.txt`, `sony_wf1000xm5.txt`, `msi_coppel_2719.txt`, `airpods_officedepot_599.txt`, `iphone_16_pro_max_liverpool_3899.txt`, `gigabyte_b450_amazon_611.txt`, `galaxy_s24_sears_1399.txt`, `galaxy_a32_walmart_197.txt`, `ipad_mini_sams_4999.txt`, `mercadolibre_link_should_be_ignored.txt`.

### Tests añadidos (Fase 3.2) — 35 tests nuevos

Distribuidos en 4 archivos:

`tests/unit/telegram/test_link_resolver.py` (6):
- `test_telegram_resolves_shortlinks_with_mock_transport` ✅
- `test_telegram_handles_link_resolver_timeout` ✅
- `test_resolver_detects_mercadolibre_after_redirect` ✅
- `test_resolver_skips_redirects_for_direct_url` ✅
- `test_resolver_uses_sqlite_cache` ✅
- `test_helpers` ✅

`tests/unit/telegram/test_candidate_builder.py` (15):
- `test_telegram_price_error_signal_gets_high_confidence` (parametrizado sobre 10 fixtures) ✅
- `test_telegram_does_not_create_candidate_for_noise` ✅
- `test_telegram_ignores_mercadolibre_links` ✅
- `test_telegram_ignores_shortlink_resolved_to_mercadolibre` ✅
- `test_telegram_91_percent_discount_creates_actionable_candidate` ✅
- `test_telegram_no_image_no_outbox_item_safe` ✅

`tests/unit/telegram/test_message_parser.py` ampliado (+7 nuevos):
- `test_telegram_detects_price_error_terms` ✅
- `test_telegram_detects_urgency_terms` ✅
- `test_telegram_extracts_price_from_message` ✅
- `test_telegram_extracts_discount_percent` ✅
- `test_telegram_extracts_store_guess` ✅
- `test_telegram_extracts_hashtags` ✅
- `test_telegram_mercadolibre_fixture_marked_as_skip` ✅

`tests/unit/agents/test_telegram_listener_agent.py` (7):
- `test_telegram_creates_candidate_signal` ✅
- `test_telegram_message_dedupe_by_chat_and_message_id` ✅
- `test_telegram_backfill_does_not_duplicate_messages` ✅
- `test_telegram_missing_credentials_fails_gracefully` ✅
- `test_telegram_disabled_returns_empty` ✅
- `test_telegram_ignores_mercadolibre_links_via_agent` ✅
- `test_telegram_ignores_shortlink_resolved_to_ml` ✅

### Suite completa

```
pytest -q
117 passed in 0.47s
```

### Comandos validados

```powershell
# 1) Verificar config (no toca Telegram)
python -m ofertas_hunter telegram-check-config

# 2) Parsear un fixture local sin tocar red
python -m ofertas_hunter telegram-parse-sample iphone_16_pro_max_liverpool_3899 --image-path /fake/x.jpg
# salida:
#   marketplace: liverpool, brand: apple, category: smartphone_flagship
#   urgency_score: 40, is_price_error: True, score: 95, classification: price_error_confirmed
#   internal: telegram_price_error_signal, outbox_type: price_error, requires_live: True

# 3) Verificar que ML link se ignora correctamente
python -m ofertas_hunter telegram-parse-sample mercadolibre_link_should_be_ignored --image-path /img.jpg
# internal: ignored_mercadolibre_from_telegram

# 4) Backfill real (requiere TELEGRAM_ENABLED=true y credenciales)
python -m ofertas_hunter telegram-backfill --limit 20

# 5) Listener en vivo
python -m ofertas_hunter telegram-listen
python -m ofertas_hunter telegram-listen --once
.\scripts\run_telegram_listener.ps1 -Once
.\scripts\run_telegram_listener.ps1 -Backfill -Limit 50

# Linux
./scripts/run_telegram_listener.sh
./scripts/run_telegram_listener.sh --once --limit 20

# 6) Sin TELEGRAM_ENABLED, los comandos terminan limpios sin tocar Telethon:
python -m ofertas_hunter telegram-listen --once
# > "TELEGRAM_ENABLED=false — el listener no se conectará."
```

### Flujo Telegram → candidate → scorer → outbox pending_revalidation

```
[Telethon NewMessage]
        │
        ▼
TelethonAdapter -> IncomingMessage(chat_id, channel, message_id, text, date, image_path)
        │
        ▼
TelegramListenerAgent._process_one(msg)
        ├─ dedupe? SELECT FROM telegram_messages WHERE channel=? AND message_id=?
        │     └─ duplicate=True -> return ProcessingOutcome(persisted=False)
        │
        ├─ parse_message(text, ...)
        │     └─ ParsedTelegramMessage(marketplace, brand, category, urgency_terms,
        │        urgency_score, is_price_error_keyword, original_url, hashtags, ...)
        │
        ├─ LinkResolver.resolve(original_url)   # HEAD → GET, max 5 redirects, cache SQLite
        │     └─ ResolvedLink(final_url, is_mercadolibre, resolved)
        │
        ├─ TelegramCandidateBuilder.build(parsed, resolved)
        │     ├─ Gate ML: si parsed.skip_reason=='mercadolibre_link' o
        │     │   resolved.is_mercadolibre -> ignored_mercadolibre_from_telegram
        │     ├─ Gate noise: sin link/precio/urgencia/discount -> noise
        │     ├─ PriceErrorScorer.score(signal) -> ScoringResult
        │     │   (Es la misma lógica determinista usada en hunters)
        │     ├─ Decisión interna:
        │     │   · is_price_error_keyword OR urgency_score>=25 OR
        │     │     score >= confirmed/possible OR discount_visible>=80
        │     │       -> telegram_price_error_signal
        │     │   · score >= 40 (suspicious) -> telegram_price_error_signal (possible_pe)
        │     │   · discount_visible >= 50% -> telegram_deal_signal
        │     │   · resto -> noise
        │     └─ Si actionable y hay precio + link, construye OutboxItem
        │        message_payload = {
        │            "title", "current_price", "url", "image_url", "marketplace",
        │            "confidence_label", "discount_percent",
        │            "requires_live_validation": True,
        │            "source": "telegram", "source_channel", "original_url",
        │            "resolved_url", "urgency_terms", "score",
        │            "score_classification", "internal_classification",
        │        }
        │
        ▼
Persistencia SQLite (en transacción):
    1) telegram_messages: dedupe key (channel, message_id), text, image_path,
       original_url, resolved_url, captured_at, processed_at, skip_reason.
    2) Si ignored_mercadolibre_from_telegram o noise: discarded_candidates.
    3) Si actionable:
       - products (UNIQUE url_canonical) -> reuse o INSERT con condition='unknown'
       - offers (state='eligible', classification=score.classification, score, reasons_json)
       - outbox (state='pending', type='price_error|possible_pe|normal',
                 message_payload con requires_live_validation=True)

        ▼
[Dispatcher de Fase 3.1]
    El item entra a outbox como pending. Cuando llegue Fase 3.3/3.4 con
    Playwright revalidator, ese revalidator confirmará producto/precio/imagen/stock
    en página real ANTES de publicar. Mientras tanto, el dispatcher en dry-run
    formatea y muestra el payload pero no envía nada.

[NUNCA se publica directo desde Telegram sin revalidación]
```

### Decisiones técnicas

- **Adapter detrás de Protocol**: `TelegramAdapter` es un `runtime_checkable` Protocol. El agent depende de la interfaz, no de Telethon. Tests usan `FakeTelegramAdapter` puro Python. Producción usa `TelethonAdapter`. Telethon **sólo se importa** cuando se instancia `TelethonAdapter` (lazy), así el resto del paquete arranca aunque Telethon no esté instalado.
- **Telethon como import lazy** se duplica en `telethon_listener_helpers.py` para no obligar al test runner a instalar Telethon.
- **Compatibilidad legacy**: `OFERTAS_TELEGRAM_API_ID` y `OFERTAS_TELEGRAM_API_HASH` se respetan vía `resolved_telegram_api_id/hash`. `TELEGRAM_CHANNELS` legacy también funciona como fallback de `TELEGRAM_TARGET_CHANNELS`.
- **`requires_live_validation=True` siempre** en payloads creados desde Telegram. El dispatcher Fase 3.1 ya tiene gates duros (image, price, url) y `Revalidator` interface; cuando llegue Fase 3.3/3.4 un `PlaywrightRevalidator` real reemplazará al `NullRevalidator` y validará página antes de publicar.
- **Ningún cambio al outbox/dispatcher** — el contrato de `OutboxItem` ya soporta este flujo con el campo extra en `message_payload`. Esto evita tocar Fase 3.1.
- **`is_shortlink/is_mercadolibre_url`** comparan host exacto con `httpx.URL`, no substring (un bug que hacía pasar `walmart.com.mx` como shortlink por contener `t.co`).
- **PUBLISHING_ENABLED y PUBLISHING_DRY_RUN siguen en false/true por defecto.** Telegram se conecta sólo si TELEGRAM_ENABLED=true.

### Criterios de aceptación cumplidos

- ✅ pytest completo: **117/117 passed**.
- ✅ Los 16 tests obligatorios listados en spec §12 cubiertos.
- ✅ Links de Mercado Libre desde Telegram se ignoran (directos y vía shortlink resuelto).
- ✅ Ejemplos extremos (laptop $305, iPhone Pro Max $3,899, Galaxy S24 $1,399, A32 $197, AirPods $599) → `internal=telegram_price_error_signal`, `confidence ∈ {high, very high}`.
- ✅ Mensajes sin link/precio/urgencia → `noise`, no genera candidato ni outbox.
- ✅ Duplicados (mismo `channel`+`message_id`) no se procesan dos veces (test verificado).
- ✅ Listener desactivado (TELEGRAM_ENABLED=false) no rompe nada — comando termina con mensaje claro.
- ✅ Conexión real a Telegram queda detrás de TELEGRAM_ENABLED.
- ✅ No se activa publicación real (PUBLISHING_ENABLED=false en `.env.example`).
- ✅ `requires_live_validation=true` en todos los payloads creados desde Telegram.

### Pendiente — Fase 3.3 (amazon_hunter Playwright)

A iniciar después de tu visto bueno. Esta fase incluirá un `PlaywrightRevalidator` que reemplazará al `NullRevalidator` para items con `requires_live_validation=True`.


---

## 2026-05-25 — Fase 3.3 (amazon_hunter Playwright + PlaywrightRevalidator) ✅

### Archivos creados

- `src/ofertas_hunter/marketplaces/__init__.py`, `base.py`, `url_utils.py` — `ExtractedProduct` (modelo común) + `extract_asin`, `canonicalize_amazon_url`, `is_amazon_url`.
- `src/ofertas_hunter/extraction/__init__.py`, `price_parser.py`, `amazon_product_parser.py` — utilidades de precios reutilizables y parser BS4 con fallbacks JSON-LD / OpenGraph / Twitter Card. Detección de mensualidad, variant_mismatch (Jaccard + cobertura del título esperado), out-of-stock, etc.
- `src/ofertas_hunter/browser/__init__.py`, `browser_context.py`, `playwright_worker.py` — `BrowserWorker` Protocol (para tests fakes), `BrowserConfig`, `RenderedPage`, y `PlaywrightBrowserWorker` con stealth, rotación UA/viewport, jitter, bloqueo de `media`/`font`, captura de screenshot en fallo, detección de captcha.
- `src/ofertas_hunter/revalidation/__init__.py`, `playwright_revalidator.py` — implementa `Revalidator` del dispatcher. Usa BrowserWorker + AmazonProductParser + PriceErrorScorer. Decide `still_eligible` y construye nuevo `payload` con datos frescos de la página (precio, imagen, asin, in_stock, score). Guarda snapshot en `dom_snapshots` cuando falla.
- `src/ofertas_hunter/agents/amazon_hunter_agent.py` — Agente hunter que itera URLs, fetchea, parsea, persiste (`products`, `price_observations`, `offers`, `outbox` o `discarded_candidates`), guarda snapshot DOM en `dom_snapshots` cuando falla.

- `scripts/test_playwright.py` — `--smoke` (no Amazon) y `--url` para validar una URL real.
- `scripts/run_amazon_hunter.sh`, `scripts/run_amazon_hunter.ps1` — wrappers VPS y Windows.
- `scripts/revalidate_outbox.py` — wrapper para `python -m ofertas_hunter revalidate-outbox`.

- `config/seeds/amazon.json` — placeholder vacío. Se debe llenar manualmente o vía `--seed`.

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - 5 nuevos subcomandos: `amazon-parse-url`, `amazon-validate-url`, `amazon-hunt`, `revalidate-outbox`, `revalidate-url`.
  - Lazy imports de Playwright/BrowserWorker para que el CLI siga arrancando sin Playwright instalado.

- `changes.md` — log de Fase 3.3.

### Lo que se portó de AmazonScrapperIA

| Origen legacy | Destino actual | Acción |
|---|---|---|
| `src/price_parser.parse_price_text` | `extraction/price_parser.parse_price_text` | REUTILIZADO con misma semántica MX/europeo |
| `src/price_parser.calculate_discount` | `extraction/price_parser.calculate_discount` | REUTILIZADO |
| `src/price_parser.extract_discount_from_text` | `extraction/price_parser.extract_discount_percent` | REUTILIZADO (renombrado) |
| `src/price_parser.extract_product_data_from_page` (Playwright) | `extraction/amazon_product_parser.AmazonProductParser.parse` (BS4 puro) | **REESCRITO** — separación de Playwright vs parsing puro para tests |
| `src/browser_worker.STEALTH_SCRIPT` | `browser/browser_context.STEALTH_SCRIPT` | REUTILIZADO (simplificado) |
| `src/browser_worker.USER_AGENTS` y `VIEWPORTS` | `browser/browser_context.USER_AGENTS`, `VIEWPORTS` | REUTILIZADO |
| `src/browser_worker.BrowserWorker` (run_session, captcha streak, etc.) | `browser/playwright_worker.PlaywrightBrowserWorker` | **REESCRITO** — solo fetch por URL, no exploración masiva (fase 4) |
| `src/dom_healer.DegradationMonitor` | n/a por ahora | DESCARTADO en Fase 3.3 (lo retomamos en Fase 4 como `self_healing/dom_healer.py`) |
| `src/memory_store` | n/a | DESCARTADO (memoria ya está en SQLite) |
| `config/selectors.json` | inline en `amazon_product_parser.py` | DESCARTADO el JSON externo: la spec exige fallbacks robustos, no selectores configurables que se rompan al editar |
| `config/seeds.json` | `config/seeds/amazon.json` | PORTADO vacío; el operador llena cuando lo desee |
| `data/heal_samples/sample_*.html` (97) | n/a | NO PORTADO en bulk; los fixtures en `tests/fixtures/amazon/` son sintéticos pero realistas y deterministas |
| `scraper.py`, `run_with_kiro.py`, `mcp_server.py`, `setup_kiro_login.py` | n/a | DESCARTADOS (no necesarios para Fase 3.3) |

**No se borró nada** del proyecto `AmazonScrapperIA`.

### Tests añadidos (Fase 3.3) — 58 tests nuevos

`tests/unit/extraction/test_price_parser.py` (28):
- `test_parse_price_text` parametrizado (9 casos)
- `test_extract_discount_percent` parametrizado (7 casos)
- `test_calculate_discount_basic` (4 casos)
- `test_detect_monthly_payment` parametrizado (8 casos)

`tests/unit/extraction/test_amazon_product_parser.py` (17):
- `TestUrlUtils`: `test_amazon_extracts_asin_from_url`, `test_amazon_normalizes_canonical_url`, `test_is_amazon_url`
- `TestAmazonExtraction`: `test_amazon_extracts_title`, `test_amazon_extracts_current_price`, `test_amazon_extracts_previous_price`, `test_amazon_calculates_discount_percent`, `test_amazon_extracts_main_image`, `test_amazon_extracts_availability`, `test_amazon_jbl_is_publishable`
- `TestAntiFalsePositives`: `test_amazon_detects_monthly_payment_not_total_price`, `test_amazon_out_of_stock_not_publishable`, `test_amazon_no_image_not_publishable`, `test_amazon_no_price_not_publishable`, `test_amazon_detects_variant_mismatch`, `test_amazon_dom_broken_has_warnings`
- `TestFallbacks`: `test_amazon_fallback_og_image`, `test_amazon_fallback_json_ld`
- `TestExtremeCases`: `test_amazon_iphone_extreme_low_price_extracted_correctly`

`tests/unit/revalidation/test_playwright_revalidator.py` (7):
- `test_revalidator_revalidates_telegram_item` ✅
- `test_revalidator_confirms_price_error_bypass` ✅
- `test_revalidator_confirms_normal_offer_cooldown` ✅
- `test_revalidator_revalidates_items_older_than_one_hour` ✅
- `test_revalidator_discards_expired_offer` ✅
- `test_revalidator_handles_captcha` ✅
- `test_revalidator_saves_snapshot_when_dom_fails` ✅

`tests/unit/agents/test_amazon_hunter_agent.py` (6):
- `test_amazon_hunter_creates_price_observation` ✅
- `test_amazon_hunter_enqueues_offer_over_50_percent` ✅
- `test_amazon_hunter_enqueues_price_error` ✅
- `test_amazon_hunter_discards_out_of_stock` ✅
- `test_amazon_hunter_dom_failure_saves_snapshot` ✅
- `test_amazon_hunter_handles_captcha` ✅

### Fixtures HTML

Todos sintéticos pero realistas (`tests/fixtures/amazon/`):
- `jbl_normal_offer.html` — caso feliz (selectores normales + JSON-LD + OG).
- `iphone_extreme_low_price.html` — error de precio extremo.
- `monthly_payment_only.html` — mensualidad / MSI.
- `out_of_stock.html` — sin stock.
- `no_image.html` — sin imagen.
- `dom_broken.html` — selectores rotos completamente.
- `og_image_fallback.html` — fuerza fallback a `og:image`.
- `json_ld_fallback.html` — sólo JSON-LD disponible.

### Suite completa

```
pytest -q
175 passed in 0.79s
```

Cobertura de los 25 tests obligatorios listados en spec §14:

| Test obligatorio | Estado |
|---|---|
| test_amazon_extracts_current_price | ✅ |
| test_amazon_extracts_previous_price | ✅ |
| test_amazon_calculates_discount_percent | ✅ |
| test_amazon_extracts_main_image | ✅ |
| test_amazon_extracts_title | ✅ |
| test_amazon_extracts_availability | ✅ |
| test_amazon_extracts_asin_from_url | ✅ |
| test_amazon_normalizes_canonical_url | ✅ |
| test_amazon_detects_monthly_payment_not_total_price | ✅ |
| test_amazon_detects_variant_mismatch | ✅ |
| test_amazon_no_image_not_publishable | ✅ |
| test_amazon_no_price_not_publishable | ✅ |
| test_amazon_out_of_stock_not_publishable | ✅ |
| test_amazon_dom_failure_saves_snapshot | ✅ |
| test_amazon_fallback_og_image | ✅ |
| test_amazon_fallback_json_ld | ✅ |
| test_revalidator_revalidates_telegram_item | ✅ |
| test_revalidator_revalidates_items_older_than_one_hour | ✅ |
| test_revalidator_confirms_price_error_bypass | ✅ |
| test_revalidator_confirms_normal_offer_cooldown | ✅ |
| test_revalidator_discards_expired_offer | ✅ |
| test_amazon_hunter_creates_price_observation | ✅ |
| test_amazon_hunter_enqueues_offer_over_50_percent | ✅ |
| test_amazon_hunter_enqueues_price_error | ✅ |
| test_revalidator_handles_captcha (extra) | ✅ |
| test_revalidator_saves_snapshot_when_dom_fails (extra) | ✅ |
| test_amazon_hunter_discards_out_of_stock (extra) | ✅ |
| test_amazon_hunter_handles_captcha (extra) | ✅ |

### Cómo validar una URL de Amazon manualmente

```powershell
# 1. Smoke test: ¿Playwright instalado y funcional?
pip install playwright
playwright install chromium
python scripts/test_playwright.py --smoke

# 2. Validar una URL específica (sin tocar SQLite):
python -m ofertas_hunter amazon-parse-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8"
# o con exit code != 0 si no es publicable:
python -m ofertas_hunter amazon-validate-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8"

# 3. Modo no-headless (debugging visual):
python scripts/test_playwright.py --url "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --no-headless

# 4. Revalidar como si fuera un item del outbox:
python -m ofertas_hunter revalidate-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --expected-title "JBL Tune 510BT"
```

### Cómo correr el hunter Amazon

```powershell
python -m ofertas_hunter init-db   # primera vez

# Con seeds específicos:
python -m ofertas_hunter amazon-hunt --seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --limit 1
.\scripts\run_amazon_hunter.ps1 -Seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" -Limit 1

# Linux:
./scripts/run_amazon_hunter.sh --seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --limit 1
```

### Cómo revalidar el outbox

```powershell
# Revalida hasta 10 items pendientes (fetcheando con Playwright). NO publica.
python -m ofertas_hunter revalidate-outbox --limit 10

# Output por item:
#   OK   outbox_id=12   type=price_error  score_class=price_error_confirmed  confidence=very high
#   BAD  outbox_id=15   fatal=out_of_stock  reasons=['out_of_stock']
```

### Flujo completo Telegram → outbox → revalidator → dispatcher dry-run

```
Mensaje Telegram (ej: ERROR DE PRECIO iPhone 16 Pro Max $3,899)
        │
        ▼
TelegramListenerAgent (Fase 3.2)
   ├─ parse_message(): marketplace=amazon, brand=apple, category=smartphone_flagship,
   │                   urgency_score=40, is_price_error_keyword=True
   ├─ LinkResolver: bit.ly → https://www.amazon.com.mx/dp/B0XXXXXXXX
   ├─ TelegramCandidateBuilder.build():
   │     classification=telegram_price_error_signal
   │     PriceErrorScorer score = 95 (very high)
   │     payload con requires_live_validation=True
   └─ persiste: telegram_messages, products, offers (state=eligible),
      outbox (state=pending, type=price_error)

[Tiempo pasa: outbox_age > 1h]
        │
        ▼
OutboxDispatcher (Fase 3.1, ahora con PlaywrightRevalidator inyectado)
   tick():
       ├─ revalidate_old_items(): item.enqueued_at > 1h → revalidator.revalidate(item)
       │   PlaywrightRevalidator (Fase 3.3):
       │     ├─ browser.fetch(item.url) → RenderedPage(html, status=200)
       │     ├─ AmazonProductParser.parse(html, url, expected_title=...)
       │     │     ExtractedProduct(title="iPhone 16 Pro Max...", current_price=3899,
       │     │                      previous_price=32999, discount_percent=88,
       │     │                      image_url=..., in_stock=True, is_publishable=True)
       │     ├─ scorer.score(signal) → score=95, classification=price_error_confirmed
       │     ├─ decide_outbox_type → "price_error" (bypass cooldown)
       │     └─ build_payload con datos reales y requires_live_validation=False
       │
       ├─ outbox.update_after_revalidation(item, new_payload)
       ├─ pick_random_eligible() → item (PE prioridad alta)
       └─ publisher.publish(item)
              ├─ formatter.format_price_error(...)
              └─ EvolutionClient.send_media(...) [DRY-RUN]
                   logger: "[DRY-RUN] sendMedia to 120363@g.us | 🚨 ERROR DE PRECIO 🚨..."
                   PUBLISHING_ENABLED=false → Outcome(skipped=True) o
                   PUBLISHING_DRY_RUN=true → success=True dry_run=True

[NO se envía nada a WhatsApp real porque PUBLISHING_DRY_RUN=true.]
[published_messages registra el "envío" simulado con success=1.]
```

### Decisiones técnicas

- **Parser separado de Playwright**: el `AmazonProductParser` recibe `html: str` y devuelve `ExtractedProduct`, sin tocar Playwright. Esto permite tests deterministas con fixtures HTML y desacopla la lógica de extracción de la red.
- **`BrowserWorker` Protocol**: tests usan `FakeBrowserWorker` con `dict[str, RenderedPage]`. El `PlaywrightBrowserWorker` real implementa la misma interfaz y se importa lazy.
- **JSON-LD + OG como fallbacks reales**: si Amazon cambia los selectores `#corePrice_desktop` o `#savingsPercentage`, JSON-LD y `og:image` cubren el caso. El test `test_amazon_fallback_json_ld` lo demuestra.
- **Variant mismatch defensivo**: el matching usa cobertura del expected_title (≥70%) o Jaccard ≥0.3. El parser sólo lo aplica si el caller pasa `expected_title` (Telegram lo hace, hunter no).
- **Mensualidad detectada por dos vías**: regex en el texto del precio (`/mes`, `MSI`, `12 pagos`) + zona DOM (`#installmentCalculator_feature_div`).
- **`PlaywrightRevalidator` cumple con la interfaz `Revalidator` de Fase 3.1**: el dispatcher no necesita conocer detalles. Si Mercado Libre se suma en Fase 3.4, basta con extender `_parse()` para enrutar al parser correspondiente.
- **`requires_live_validation=False`** se setea en el payload tras revalidación exitosa, así próximos ticks no la repiten.
- **PUBLISHING_ENABLED=false y PUBLISHING_DRY_RUN=true** siguen intactos. Telegram_enabled también sigue en false. Toda la fase 3.3 se valida con fixtures locales y mocks; las pruebas reales requieren `pip install playwright && playwright install chromium` y usar los comandos `amazon-parse-url`, `amazon-validate-url`, `revalidate-url` o `amazon-hunt --seed <url>`.

### Criterios de aceptación cumplidos

- ✅ pytest completo: **175/175 passed** (Fase 1+2+3.1+3.2+3.3).
- ✅ No se rompió Fase 3.1 ni Fase 3.2.
- ✅ Parser Amazon funciona con fixtures HTML.
- ✅ Revalidator puede validar un item de Telegram antes de publicación.
- ✅ Items de Telegram quedan `requires_live_validation=True` hasta validarse.
- ✅ Item Amazon con imagen/precio/stock/descuento ≥50 entra al outbox como NORMAL.
- ✅ Error de precio confirmado entra como PRICE_ERROR con bypass cooldown.
- ✅ No se publica nada real (`PUBLISHING_DRY_RUN=true`).
- ✅ Si falla DOM, se guarda snapshot en `dom_snapshots` y razón en `discarded_candidates`.
- ✅ `changes.md` actualizado.

### Pendiente — Fase 3.4

`mercadolibre_hunter` con cookies persistentes. El `PlaywrightRevalidator` ya tiene la rama lista para añadir un `MercadoLibreProductParser` (ver `_parse()` con `if marketplace == "amazon"`).


---

## 2026-05-25 — Fase 3.4 (Mercado Libre + afiliados) ✅

### Auditoría honesta

Inicialmente porté schema/modelos con `affiliate_link, affiliate_product_id, commission_text` (heredados del audit) pero **olvidé portar la lógica real** de extracción del modal Compartir. El usuario me lo señaló y lo arreglé en este mismo bloque, antes de cerrar Fase 3.4.

**Origen de la lógica afiliada en el legacy:**
- `bot_diversidad_global/src/browser_worker.py::extract_affiliate_link()` (líneas ~140-220).
- Click `[data-testid="generate_link_button"]` → espera modal "Generar link" → polling sobre `[data-testid="text-field__label_link"]` (textarea con `meli.la/...`) y `[data-testid="text-field__label_id"]`.
- Lee `commission_text` de `.stripe-commission__info span` antes del click.
- **NO usa OAuth API de ML**. El script `scripts/check_oauth_token.sh` del legacy referencia `/opt/ofertas-detector-vps/data/mercadolibre/oauth_tokens.json` que es de **otro proyecto** del VPS, no del crawler ML.
- `scripts/enrich_affiliate_links.py`: backfill one-shot (descartado correctamente).
- `scripts/test_affiliate_link.py`: test manual con BrowserWorker (su lógica se reemplaza por `scripts/test_playwright.py` + `ml-validate-url`).

### Archivos creados / modificados (afiliados)

- `src/ofertas_hunter/marketplaces/mercadolibre_affiliate.py` — `AffiliateExtractor` (Protocol) + `PlaywrightAffiliateExtractor` real (lazy import). Devuelve `AffiliateInfo(affiliate_url, affiliate_product_id, commission_text, success, error)`. Reproduce exactamente el flujo del legacy.
- `src/ofertas_hunter/agents/mercadolibre_hunter_agent.py` — extendido con:
  - parámetros `affiliate_extractor` y `affiliate_required_for_publish`.
  - `_maybe_extract_affiliate(product)`: llama al extractor sólo si hay share button.
  - Si `affiliate_required_for_publish=True` y no hay `affiliate_url` → descarta con razón `missing_affiliate_url`.
  - `_update_product_affiliate(product_id, info)`: actualiza `products.affiliate_link/affiliate_product_id/commission_text`.
  - Payload del outbox incluye `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url`, y `url=affiliate_url or canonical_url`.
- `src/ofertas_hunter/publishing/whatsapp_publisher.py` — extendido con `mercadolibre_affiliate_required: bool=True`. Si es `mercadolibre` y falta `affiliate_url`, retorna `error="missing_affiliate_url"` sin llamar a Evolution. El formatter usa `affiliate_url > url > canonical_url`.
- `src/ofertas_hunter/revalidation/playwright_revalidator.py` — `_build_payload` conserva `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url` del payload original tras revalidar (la revalidación NO regenera el modal).
- `src/ofertas_hunter/config.py` — `mercadolibre_affiliate_required_for_publish=True`, `mercadolibre_affiliate_timeout_seconds=15.0` y otras vars de ML.
- `.env.example` — vars nuevas con compatibilidad legacy.

### Tests añadidos (afiliados — todos verde)

`tests/unit/agents/test_mercadolibre_hunter_agent.py`:
- `test_ml_hunter_requests_affiliate_before_outbox` — el hunter llama al extractor con `canonical_url`. ✅
- `test_ml_outbox_payload_contains_affiliate_url` — `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url` están en el payload. ✅
- `test_ml_canonical_url_is_used_for_revalidation` — `canonical_url` (sin `meli.la`) se conserva. ✅
- `test_ml_missing_affiliate_blocks_publication` — sin afiliado + `required=True` → `discarded_reason="missing_affiliate_url"`. ✅
- `test_ml_missing_affiliate_allowed_when_not_required` — sin afiliado + `required=False` → entra al outbox. ✅
- `test_ml_no_share_button_skips_affiliate_extraction` — sin share button no se llama al extractor (devuelve `no_share_button` sintético). ✅

`tests/unit/publishing/test_whatsapp_publisher_ml.py`:
- `test_whatsapp_uses_affiliate_url_for_ml` — el mensaje contiene `meli.la/...`, no la canonical_url. ✅
- `test_whatsapp_blocks_ml_without_affiliate` — gate ML afiliado bloquea publicación con `error="missing_affiliate_url"`. ✅
- `test_whatsapp_allows_ml_without_affiliate_when_not_required` — gate desactivado → publica con canonical. ✅
- `test_whatsapp_amazon_does_not_require_affiliate` — gate sólo aplica a `marketplace=mercadolibre`. ✅

`tests/unit/revalidation/test_mercadolibre_revalidator.py`:
- `test_ml_revalidator_preserves_affiliate_url` — tras revalidar, el payload conserva `affiliate_url`, `affiliate_product_id`, `commission_text`. ✅
- `test_ml_revalidator_uses_canonical_for_fetch_when_available` — fetchea `resolved_url` (canonical), no la URL afiliada. ✅
- `test_ml_revalidator_blocks_telegram_source` — `source=telegram` + ML → `discard_reason="mercadolibre_link_from_telegram"`. ✅
- `test_ml_revalidator_confirms_normal_offer` — revalidación exitosa funciona con ML. ✅

`tests/unit/telegram/test_candidate_builder.py`:
- `test_ml_telegram_source_links_remain_ignored_no_affiliate` — links ML desde Telegram nunca llegan al hunter ML, por lo que nunca generan `affiliate_url`. ✅

### Suite final

```
pytest -q
230 passed in 1.29s
```

### Reglas duras finales

1. **ML desde Telegram**: ignorado por `TelegramCandidateBuilder` (`internal_classification=ignored_mercadolibre_from_telegram`). Nunca llega al hunter ML, nunca genera afiliado, nunca se publica.
2. **ML desde hunter propio**: pasa por `MercadoLibreHunterAgent` → llama a `AffiliateExtractor` si hay share button → si hay afiliado, lo guarda en `products` y `outbox.payload`.
3. **`MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`** (default): un item ML sin `affiliate_url` se descarta en el hunter (`missing_affiliate_url` en `discarded_candidates`). Si por alguna razón llegara al publisher, el gate ML del publisher lo bloquearía con `error="missing_affiliate_url"` antes de tocar Evolution.
4. **Publicación WhatsApp**: el mensaje usa `affiliate_url > url > canonical_url`. La URL `meli.la/...` aparece literalmente en el mensaje del grupo.
5. **Revalidación**: `canonical_url` (no `affiliate_url`) se usa para fetchear contra ML. `affiliate_url` se conserva del payload original sin regenerar (eso requiere otro fetch + click + cookies del afiliado).

### Lo que sigue igual sin cambio

- `PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true` (default).
- `TELEGRAM_ENABLED=false` (default).
- Cero credenciales reales activadas.
- AmazonScrapperIA y bot_diversidad_global intactos.


---

## 2026-05-25 — Fase 3.4 cierre con `ml-enrich-affiliates` ✅

### Resumen

Completo Fase 3.4 con el comando solicitado para enriquecer afiliados en items existentes del outbox. **Una sola lógica reutilizada**: el servicio `MercadoLibreAffiliateEnricher` es invocado tanto por el CLI como por el script wrapper. Cero duplicación.

### Archivos creados

- `src/ofertas_hunter/agents/mercadolibre_affiliate_enricher.py` — servicio puro:
  - `MercadoLibreAffiliateEnricher(conn, extractor)` con método `run(limit)`.
  - `EnrichmentReport(total_candidates, enriched, failed, skipped, outcomes)`.
  - `EnrichmentOutcome(outbox_id, canonical_url, affiliate_url, ..., status)`.
  - SELECT filtrando ML + no-Telegram + sin `affiliate_url`.
  - Persiste éxito en `outbox.message_payload_json` (campos `affiliate_url`, `affiliate_product_id`, `commission_text`, `url=affiliate_url`, `affiliate_status="ok"`, `affiliate_enriched_at`) y refleja en `products` si url_canonical existe.
  - Persiste fallo dejando `affiliate_status="failed"` (o `"pending"` si extractor=None) y `affiliate_error=<motivo>`. **No publica** ni cambia `outbox.state` — sólo añade metadata.

- `scripts/enrich_affiliate_links.py` — wrapper que delega a `python -m ofertas_hunter ml-enrich-affiliates`. Reutiliza la misma función `main()` del paquete: cero duplicación.

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - Nuevo subcomando `ml-enrich-affiliates [--limit N] [--no-headless] [--no-extractor]`.
  - El handler arma Playwright + cookies de `MercadoLibreSession` y crea `PlaywrightAffiliateExtractor`. Si Playwright no está, marca todos los candidatos con `status=pending` para reintento.
  - `--no-extractor` permite ejecutar el comando sin Playwright (útil para QA, CI, o cuando se quiere encolar pendientes a revisar).

### Tests añadidos (7, todos verde)

`tests/unit/agents/test_mercadolibre_affiliate_enricher.py`:
- `test_ml_enrich_affiliates_skips_telegram_source` — items con `source=telegram` ni siquiera entran a candidatos. ✅
- `test_ml_enrich_affiliates_skips_items_with_existing_affiliate_url` — items que ya traen `affiliate_url` se preservan intactos. ✅
- `test_ml_enrich_affiliates_updates_outbox_payload` — éxito: payload incluye `affiliate_url, affiliate_product_id, commission_text, affiliate_status="ok", affiliate_enriched_at`; `url=affiliate_url`; `canonical_url` se conserva; `products` se actualiza. ✅
- `test_ml_enrich_affiliates_records_failure` — fallo: payload trae `affiliate_status="failed"`, `affiliate_error="modal_textareas_empty"`, `url` sigue siendo canonical, no se setea `affiliate_url`. ✅
- `test_ml_enrich_affiliates_marks_pending_when_no_extractor` — sin Playwright, los items se marcan como `status=pending`/`error="no_extractor"` para reintento. ✅
- `test_ml_enrich_affiliates_respects_limit` — `--limit N` selecciona los N primeros candidatos. ✅
- `test_ml_enrich_affiliates_skips_non_mercadolibre` — items de Amazon u otros marketplaces no entran a candidatos. ✅

### Suite final

```
pytest -q
237 passed in 1.37s
```

### Validación end-to-end del CLI

```powershell
python -m ofertas_hunter init-db
python -m ofertas_hunter ml-enrich-affiliates --no-extractor --limit 5
# --- ml-enrich-affiliates ---
#   candidates: 0
#   enriched:   0
#   failed:     0
#   skipped:    0
```

(En una DB vacía no hay candidatos; con items reales el comando llamaría a Playwright + cookies y actualizaría el payload de cada uno.)

### Cómo ejecutarlo

```powershell
# CLI directo
python -m ofertas_hunter ml-enrich-affiliates --limit 20

# Script wrapper (mismo servicio interno)
python scripts/enrich_affiliate_links.py --limit 20

# Modo QA sin Playwright (marca todos pending)
python -m ofertas_hunter ml-enrich-affiliates --no-extractor --limit 50

# Modo debugging visual (browser visible)
python -m ofertas_hunter ml-enrich-affiliates --limit 5 --no-headless
```

### Reglas confirmadas

1. ✅ Sólo procesa items `marketplace=mercadolibre`.
2. ✅ Sólo procesa items `source != telegram`.
3. ✅ Sólo procesa items sin `affiliate_url`.
4. ✅ Usa `canonical_url` para abrir el producto (no la URL afiliada).
5. ✅ Guarda `affiliate_url, affiliate_product_id, commission_text` y `affiliate_status="ok"` en éxito.
6. ✅ Guarda `affiliate_status="failed"` o `"pending"` y `affiliate_error=<motivo>` en fallo.
7. ✅ **No publica nada**: solo modifica metadata del payload del outbox.
8. ✅ El gate ML del publisher (`mercadolibre_affiliate_required_for_publish=true`) sigue protegiendo: items sin afiliado no se publican aunque el dispatcher los pickee.

### Estado de la Fase 3.4

**Cerrada.** Todo lo exigido está implementado, testeado y documentado:
- Parser ML con cookies, share button, condition (used/refurbished), monthly payment, variant mismatch.
- Extractor real del modal Compartir (Playwright lazy import).
- Hunter ML que llama al extractor antes del outbox y enforza `affiliate_required_for_publish`.
- Revalidator que conserva afiliado tras revalidación y bloquea ML desde Telegram.
- Publisher con gate dedicado para ML que falla con `missing_affiliate_url` cuando aplica.
- CLI `ml-enrich-affiliates` + script wrapper.
- 237 tests verde (incluyendo 36 nuevos de Fase 3.4 + 7 del enricher).
- `PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true`, `TELEGRAM_ENABLED=false` intactos.
- `MERCADOLIBRE_ENABLED=false` por default (se activa con cookies + extractor + decision del operador).

### Próxima fase

Fase 4 (watchdog, self-healing, memory compressor, deploy systemd) o lo que el operador defina.


---

## 2026-05-25 — Fase 4 (watchdog + self-healing + memory + deploy) ✅

Cuatro bloques implementados sin tocar Telethon, Playwright real ni envío real. Todo testeado con clocks inyectables y DBs temporales.

### Fase 4.1 — Runtime Watchdog + Heartbeat

`src/ofertas_hunter/runtime/`:
- `heartbeat.py` — `AgentRunRegistry(conn, clock=...)` que persiste `agent_runs(started_at, status, last_heartbeat, ended_at)`. Clock inyectable para tests.
- `events.py` — `emit_runtime_event(conn, kind, severity, payload)` central.
- `watchdog.py` — `RuntimeWatchdog` con:
  - Registro de `SupervisedAgent(name, factory)`.
  - Tick que detecta `task.done()` o heartbeat viejo > `stale_after_seconds` y reinicia con backoff exponencial.
  - Si excede `max_restarts_in_window` → `agent.degraded=True`, registra `runtime_event(severity=critical, kind="agent_degraded")` y deja de reintentar.
  - `shutdown_all()` cancela tareas vivas y marca `agent_runs.status="killed"`.

12 tests verde (`tests/unit/runtime/`).

### Fase 4.2 — DOM Healer con tests automáticos antes de patch

`src/ofertas_hunter/self_healing/`:
- `degradation_monitor.py` — `DegradationMonitor(window, threshold, min_samples)`. Por `(marketplace, context)`, dispara `is_degraded=True` cuando la tasa de fallos supera `threshold`.
- `selector_versioner.py` — registra `selector_versions(marketplace, context, key, selector_value, applied_by, test_pass, fixture_path, reverted_at)`.
- `dom_healer.py` — `DomHealer.heal(marketplace, context, url, html)`:
  1. Guarda `dom_snapshots`.
  2. `HeuristicSelectorRecovery` extrae `current_price` (JSON-LD), `image_url` (og:image), `title` (og:title), regex price.
  3. Persiste fixture HTML local en `data/heal_samples/<marketplace>/<context>/healed_<ts>.html`.
  4. Llama al `tester(fixture_path)` inyectado. Si pasa → `selector_versions(test_pass=1)`. Si falla → `revert(reason="tests_failed")`.
- Sin LLM real; arquitectura abierta (`tester` callable es el punto de extensión).

16 tests verde (`tests/unit/self_healing/`).

### Fase 4.3 — Memory Compressor

`src/ofertas_hunter/memory/compressor.py`:
- `CompressorConfig` con políticas por tabla:
  - `dom_snapshots_keep=200` (mantiene N más recientes)
  - `runtime_events_ttl_days=30`
  - `discarded_candidates_ttl_days=30` + `discarded_candidates_keep_max=5000`
  - `agent_runs_keep=1000`
- Genera 4 `memory_summaries`:
  - `discard_reasons` (top 10 razones por source)
  - `unstable_selectors` (>= 3 versiones en 30d)
  - `runtime_events_by_severity` (critical primero)
  - `marketplace_observations` (cuenta de `price_observations` por marketplace)
- Clock inyectable. Idempotente con DB vacía.

6 tests verde (`tests/unit/memory/`).

### Fase 4.4 — CLI + scripts de mantenimiento

`src/ofertas_hunter/__main__.py` añadió:
- `compress-memory`
- `export-memory-summary [--kind ...] [--out file]`
- `check-db` (counts de tablas + journal_mode + foreign_keys)
- `status` (env + agentes activos + outbox + runtime events críticos)
- `watchdog-tick --once` (placeholder para integración futura)

Scripts wrappers (delegan al CLI, cero duplicación):
- `scripts/check_db.py`
- `scripts/export_memory_summary.py`
- `scripts/status.sh`
- `scripts/run_worker.sh`

### Fase 4.5 — Deploy (systemd + Docker)

`deploy/systemd/`:
- `ofertas-hunter.service` — orchestrator (placeholder; Fase 5 conectará al watchdog real).
- `ofertas-hunter-dispatcher.service` — outbox dispatcher.
- `ofertas-hunter-telegram.service` — listener Telethon.
- `ofertas-hunter-maintenance.service` + `.timer` — corre `compress-memory` cada 6h.
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths` limitado.

`deploy/install.sh` — instalador idempotente que renderiza los unit files reemplazando `__PROJECT_ROOT__` y `__SERVICE_USER__`. **No arranca los servicios automáticamente** — el operador decide cuándo.

`deploy/docker/Dockerfile` — imagen Python 3.11-slim con dependencias de Chromium para Playwright. `python -m playwright install --with-deps chromium`. Volumes para `data`, `secrets`, `logs`. Entrypoint `python -m ofertas_hunter`, comando default `status`.

`deploy/docker/docker-compose.yml` — services para `dispatcher`, `telegram-listener`, `amazon-hunter-once`, `ml-hunter-once`, `maintenance` (con profiles).

### Suite final

```
pytest -q
271 passed in 3.04s
```

Distribución de tests nuevos en Fase 4:
- `tests/unit/runtime/` — 12
- `tests/unit/self_healing/` — 16
- `tests/unit/memory/` — 6

**Total Fase 4: +34 tests** (de 237 a 271 sin regresiones).

### Cómo desplegar en VPS

```bash
# 1. Como root
sudo PROJECT_ROOT=/opt/ofertas-hunter SERVICE_USER=ofertas \
     deploy/install.sh

# 2. Como el usuario de servicio
sudo -iu ofertas
cd /opt/ofertas-hunter
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python -m ofertas_hunter init-db
python -m ofertas_hunter check-db
python -m ofertas_hunter status

# 3. Habilitar timer de mantenimiento (corre compress-memory cada 6h)
sudo systemctl enable --now ofertas-hunter-maintenance.timer

# 4. Cuando estés listo (sigue dry-run por default)
sudo systemctl enable --now ofertas-hunter
sudo systemctl enable --now ofertas-hunter-dispatcher

# 5. Ver estado
sudo systemctl status ofertas-hunter ofertas-hunter-dispatcher
sudo journalctl -u ofertas-hunter -f
sudo -iu ofertas /opt/ofertas-hunter/scripts/status.sh
```

### Cómo desplegar con Docker

```bash
docker compose -f deploy/docker/docker-compose.yml build
docker compose -f deploy/docker/docker-compose.yml up -d dispatcher
# Para Telegram: requiere TELEGRAM_ENABLED=true en .env
docker compose -f deploy/docker/docker-compose.yml --profile telegram up -d
# Maintenance one-shot
docker compose -f deploy/docker/docker-compose.yml --profile maintenance run --rm maintenance
```

### Estado del proyecto

| Fase | Estado |
|---|---|
| 1 — Auditoría + diseño + schema | ✅ |
| 2 — Core | ✅ |
| 3.1 — outbox_dispatcher + Evolution | ✅ |
| 3.2 — telegram_listener Telethon | ✅ |
| 3.3 — amazon_hunter + revalidator | ✅ |
| 3.4 — mercadolibre_hunter + afiliados | ✅ |
| 4.1 — runtime_watchdog | ✅ |
| 4.2 — dom_healer + selector_versioner | ✅ |
| 4.3 — memory_compressor | ✅ |
| 4.4 — CLI + scripts mantenimiento | ✅ |
| 4.5 — deploy systemd + Docker | ✅ |

**271 tests verde. Cero regresiones desde Fase 1. Cero diagnostics.**

`PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true`, `TELEGRAM_ENABLED=false`, `MERCADOLIBRE_ENABLED=false` siguen en `.env.example` por default.

### Pendiente en `run` profile

El subcomando `python -m ofertas_hunter run` actualmente es un no-op con warning. La spec original (§8 Multiagentes + §13 Fase 4) habla de un `supervisor/orchestrator` que arranca todos los agentes registrados al watchdog. La infraestructura está lista (`RuntimeWatchdog` + `AgentRunRegistry` + factories tipadas), pero conectarlas en un único `orchestrator.py` que lea `.env` y arme las factories de los hunters/dispatcher/listener queda como **Fase 5** cuando se decida activar la operación end-to-end real.


---

## 2026-05-25 — Fase 5 (orchestrator real conectando todos los agentes) ✅

### Resumen

`python -m ofertas_hunter run` ya no es un no-op: arranca el orchestrator completo registrando los agentes habilitados en el watchdog (Fase 4.1). Cada agente respeta su flag `*_ENABLED` del `.env`. Si una dependencia opcional falta (Playwright, Telethon, cookies), el agente queda dormido en lugar de crashear.

### Archivos creados

- `src/ofertas_hunter/orchestrator.py` (510 líneas):
  - `OrchestratorConfig` con intervalos por agente, seeds, límites.
  - `AgentFactoryBuilder` que construye factories tipadas para:
    - `outbox_dispatcher` (siempre activo, drena outbox respetando flags)
    - `amazon_hunter` (sólo si `AMAZON_ENABLED=true`)
    - `mercadolibre_hunter` (sólo si `MERCADOLIBRE_ENABLED=true`, con cookies + extractor afiliado)
    - `telegram_listener` (sólo si `TELEGRAM_ENABLED=true` y credenciales presentes)
    - `maintenance` (siempre activo, corre `compress-memory` cada 6h)
  - `Orchestrator.register_agents()` → `RegistrationReport(registered, skipped)`.
  - `Orchestrator.run()` → registra agentes, emite `runtime_event(orchestrator_starting)`, arranca el watchdog en modo `--once` o continuo.
  - `load_amazon_seeds(settings)` y `load_mercadolibre_seeds(settings)` para leer `config/seeds/*.json`.
  - **Lazy imports**: Playwright/Telethon sólo se importan cuando se intenta arrancar el agente correspondiente. Si fallan, emiten `runtime_event(severity=warning, kind=agent_skipped)` y entran en loop ocioso (no crashean al watchdog).

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - `cmd_run` ahora llama a `Orchestrator.run()` con `--once` opcional.
  - Subparser de `run` con `--once` y `--limit`.
- `changes.md` — log de Fase 5.

### Comportamiento por flag

| Flag                              | Default | Efecto                                              |
|-----------------------------------|---------|-----------------------------------------------------|
| `PUBLISHING_ENABLED=false`        | ✅       | Publisher retorna `skipped=true`; items quedan pending |
| `PUBLISHING_DRY_RUN=true`         | ✅       | Si llega a publicar, simula sin tocar Evolution API |
| `TELEGRAM_ENABLED=false`          | ✅       | Listener no se registra; watchdog emite `agent_skipped` |
| `AMAZON_ENABLED=true` + sin seeds | -       | Hunter registrado pero loop ocioso                  |
| `MERCADOLIBRE_ENABLED=true` + sin cookies | -       | Hunter registrado; emite `cookie_expiry`; ML afiliado fail |

### Tests añadidos (7, todos verde)

`tests/unit/test_orchestrator.py`:
- `test_orchestrator_registers_only_enabled_agents` — flags off → skip + runtime_events.
- `test_orchestrator_registers_all_when_flags_enabled`.
- `test_orchestrator_run_once_calls_each_agent` — `--once` ejecuta una pasada por agente.
- `test_orchestrator_handles_agent_failure_gracefully` — un agente que crashea no detiene los demás; queda `agent_runs.status='error'`.
- `test_orchestrator_emits_event_with_skipped_agents` — el evento `orchestrator_starting` lista agentes skipped.
- `test_orchestrator_dispatcher_always_registered` — dispatcher + maintenance siempre activos.
- `test_orchestrator_records_agent_runs_for_each_cycle`.

Builder usado en tests: `FakeFactoryBuilder` que devuelve factories triviales (sin Playwright/Telethon). Esto deja la lógica de wiring 100% testeable.

### Suite final

```
pytest -q
278 passed in 3.22s
```

### Validación end-to-end

```powershell
# 1. DB limpia
python -m ofertas_hunter init-db

# 2. Encolar oferta de prueba
python -m ofertas_hunter enqueue-sample --kind normal

# 3. Correr orchestrator un solo ciclo
python -m ofertas_hunter run --once

# Logs:
#   [INFO] publishing_enabled=False publishing_dry_run=True — modo seguro activo
#   [WARNING] agent amazon_hunter no se registra: no_seeds_configured
#   [WARNING] agent mercadolibre_hunter no se registra: no_seeds_configured
#   [WARNING] agent telegram_listener no se registra: telegram_disabled
#   [INFO] memory_compactation: dom=0 events=0 disc=0 runs=0 summaries=[...]
#   [INFO] publish skipped: publishing_disabled
#   [INFO] Publish skipped (publishing_disabled) item=1 — se mantiene pending

# 4. Estado consolidado
python -m ofertas_hunter status
# active agents: (ninguno)  ← --once ya terminó
# outbox: normal pending 1
```

### Cómo activar operación real

Sin tocar `PUBLISHING_DRY_RUN=true` no hay envíos. Los pasos para producción:

1. Editar `.env`:
   ```
   AMAZON_ENABLED=true
   MERCADOLIBRE_ENABLED=true
   TELEGRAM_ENABLED=true                 # con credenciales válidas
   PUBLISHING_ENABLED=true
   PUBLISHING_DRY_RUN=false              # ← ÚLTIMO paso
   EVOLUTION_BASE_URL=...
   EVOLUTION_API_KEY=...
   EVOLUTION_INSTANCE=...
   OFERTAS_WHATSAPP_GROUP_ID=...
   TELEGRAM_API_ID=...
   TELEGRAM_API_HASH=...
   ```

2. Llenar `config/seeds/amazon.json` y `config/seeds/mercadolibre.json`.

3. Validar manualmente:
   ```bash
   python -m ofertas_hunter check-config
   python -m ofertas_hunter check-db
   python scripts/test_whatsapp.py --send --to 5218338498692 --text ping
   python -m ofertas_hunter run --once  # un ciclo controlado
   python -m ofertas_hunter status
   ```

4. Sólo cuando todo lo anterior pase, arrancar el servicio continuo:
   ```bash
   sudo systemctl start ofertas-hunter
   sudo journalctl -u ofertas-hunter -f
   ```

### Estado final del proyecto

| Fase | Estado |
|---|---|
| 1 — Auditoría + diseño + schema + 16 tests obligatorios | ✅ |
| 2 — Core (config, db, scorer, formatter, outbox, telegram parser) | ✅ |
| 3.1 — outbox_dispatcher + Evolution API | ✅ |
| 3.2 — telegram_listener Telethon | ✅ |
| 3.3 — amazon_hunter + PlaywrightRevalidator | ✅ |
| 3.4 — mercadolibre_hunter + afiliados + ml-enrich-affiliates | ✅ |
| 4.1 — runtime_watchdog + heartbeat | ✅ |
| 4.2 — dom_healer + selector_versioner | ✅ |
| 4.3 — memory_compressor | ✅ |
| 4.4 — CLI + scripts mantenimiento | ✅ |
| 4.5 — deploy systemd + Docker | ✅ |
| **5 — Orchestrator real con todos los agentes** | **✅** |

**278 tests verde. Cero regresiones desde Fase 1. Cero diagnostics. AmazonScrapperIA y bot_diversidad_global intactos.**

`PUBLISHING_DRY_RUN=true`, `PUBLISHING_ENABLED=false`, `TELEGRAM_ENABLED=false`, `MERCADOLIBRE_ENABLED=false`, `AMAZON_ENABLED=true` (default) siguen como están en `.env.example`. **Cero envíos reales hasta que el operador lo autorice explícitamente.**


---

## 2026-05-25 — Fase 6 (descubrimiento autónomo + credenciales legacy) ✅

El bot deja de depender de listas estáticas de productos. Ahora visita listings/categorías/deals, extrae URLs nuevas y las persiste en `frontier`. Los hunters consumen esas URLs en lugar de seeds estáticas. Resultado: ofertas frescas en cada ciclo.

### Credenciales del legacy migradas a `.env`

Heredado tal cual de `bot_diversidad_global/MEMORY.md` y `WHATSAPP_INTEGRATION.md`:

```env
EVOLUTION_BASE_URL=http://162.251.147.177:8080
EVOLUTION_API_KEY=dev-evolution-api-key
EVOLUTION_INSTANCE=mi-instancia
WHATSAPP_TARGET_GROUP_ID=120363426569734715@g.us
OFERTAS_WHATSAPP_GROUP_ID=120363426569734715@g.us

TELEGRAM_API_ID=22295300
TELEGRAM_API_HASH=611f70b50f4c98de216e7bf3c83f0b7a
TELEGRAM_TARGET_CHANNELS=ofertonesmexico,OFERTAS PREMIUM MX,OFERTAS RELAMPAGO
```

Defaults conservadores intactos: `PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true`, `TELEGRAM_ENABLED=false`. Las cookies de Mercado Libre el operador las copia manualmente desde el legacy a `secrets/mercadolibre_cookies.json`.

### Archivos creados

- `src/ofertas_hunter/exploration/__init__.py`.
- `src/ofertas_hunter/exploration/url_classifier.py` — `classify(url) -> ClassifiedUrl(marketplace, kind, score)`. Identifica `product | listing | category | deals | unknown` por marketplace. Bloquea login/help/blog/seller/registro de ML.
- `src/ofertas_hunter/exploration/frontier.py` — `FrontierRepo(conn)` sobre la tabla `frontier` ya existente. API: `add`, `add_many`, `add_classified`, `pop`, `peek`, `mark_visited`, `is_visited`, `count_pending`, `count_visited`. UNIQUE en `(marketplace, url_canonical)` evita duplicados; los visited no se reinsertan.
- `src/ofertas_hunter/exploration/listing_extractor.py` — `extract_amazon_listing`, `extract_amazon_deals`, `extract_mercadolibre_listing` (BS4 puros, testables). Reconocen `data-component-type='s-search-result'`, `data-asin`, `s-pagination-next`, `ui-search-link`, `ui-search-item__group__element`, `andes-pagination__link`.
- `src/ofertas_hunter/agents/discovery_agent.py` — `DiscoveryAgent(browser, db_conn, marketplace)`:
  - `seed_from_config(urls)` siembra categorías al frontier (idempotente).
  - `discover_once()` toma URLs `kind in {listing, category, deals}` del frontier, las fetchea, extrae productos + paginación, los persiste de vuelta. Maneja login_redirect y captcha emitiendo `runtime_event` y guardando snapshot.

### Archivos modificados

- `config/seeds/amazon.json` — 36 URLs reales (search keywords + `/deals` + `/gp/goldbox`). Heredadas y ampliadas desde el legacy `AmazonScrapperIA/config/seeds.json`.
- `config/seeds/mercadolibre.json` — 15 categorías + ofertas reales.
- `.env` — credenciales reales heredadas (Evolution + Telegram + paths). Sigue gitignored.
- `src/ofertas_hunter/agents/amazon_hunter_agent.py` — nuevo método `hunt_from_frontier(max_urls)` que consume URLs `kind=product` del frontier.
- `src/ofertas_hunter/agents/mercadolibre_hunter_agent.py` — idem.
- `src/ofertas_hunter/orchestrator.py`:
  - `make_amazon_hunter_factory` ahora arma `DiscoveryAgent + AmazonHunterAgent` y en cada ciclo:
    1. `discovery.discover_once()` → descubre productos en N listings.
    2. `hunter.hunt_from_frontier()` → procesa N productos del frontier.
  - `make_mercadolibre_hunter_factory` igual, pero con cookies + extractor afiliado.
  - Las seeds dejan de ser productos y se entienden como **puntos de entrada** (categorías).
- `scripts/validate_seeds.py` — utilidad nueva: clasifica las seeds y reporta cuáles son aceptables (sin tocar red).

### Flujo de descubrimiento autónomo

```
seeds.json (categorías/búsquedas)
        ↓
discovery_agent.seed_from_config()  → frontier(kind=listing|category|deals)
        ↓ (cada ciclo)
discovery_agent.discover_once():
   1. frontier.peek(listing/category/deals, limit=2)
   2. browser.fetch(url)
   3. extract_*_listing(html)  → productos + paginación nueva
   4. frontier.add_classified(*)  → kind=product (score=10)
        ↓
hunter.hunt_from_frontier():
   1. frontier.pop(kind=product, limit=5)
   2. parser → ExtractedProduct
   3. price_observation + offer + outbox (si publicable)
   4. discarded_candidates (si no)
        ↓
dispatcher.tick()  →  WhatsApp (dry-run por default)
```

El bot **no requiere** que el operador conozca productos específicos. Sólo le da puntos de entrada (categorías populares) y el bot encuentra productos frescos en cada ciclo.

### Tests añadidos (39, todos verde)

- `tests/unit/exploration/test_url_classifier.py` (17): clasifica URLs Amazon/ML/other; valida bloqueos (login, help, blog, signin); scores correctos.
- `tests/unit/exploration/test_frontier.py` (7): add/pop/visited/dedup/UNIQUE; isolation por marketplace; orden por score desc.
- `tests/unit/exploration/test_listing_extractor.py` (6): extracción correcta de productos por `data-asin` y selectores variados; OG/normalización de URLs relativas; dedup; HTML vacío.
- `tests/unit/agents/test_discovery_agent.py` (6): seed → frontier; descubrimiento con FakeBrowserWorker; paginación re-encolada; login_redirect emite `cookie_expiry`; captcha emite snapshot; ML extracts.

Cambios menores en tests existentes:
- ML pagination ahora se clasifica como `category` (URL `/c/...?page=2`), antes era `listing`. Test ajustado.

### Suite final

```
pytest -q
317 passed in 3.56s
```

### Validación de seeds

```
$ python scripts/validate_seeds.py
--- amazon: amazon.json (36 URLs) ---
  listing    34
  deals      2
--- mercadolibre: mercadolibre.json (15 URLs) ---
  category   13
  deals      2
=== total OK: 51  malas: 0 ===
```

### Cómo arrancar el bot autónomo

```powershell
# Una sola vez (ya hecho):
python -m ofertas_hunter init-db

# Validar seeds:
python scripts/validate_seeds.py

# Probar el orchestrator un ciclo:
python -m ofertas_hunter run --once

# Estado:
python -m ofertas_hunter status
```

Con `AMAZON_ENABLED=true` y `MERCADOLIBRE_ENABLED=true` (defaults en `.env`) y Playwright instalado, el orchestrator:

1. Lee 36+15=51 seeds.
2. En cada ciclo, descubre 2 listings/categorías y extrae los productos visibles.
3. Procesa hasta 5 productos por marketplace.
4. Si encuentra ≥50% descuento o error de precio confirmado, encola al outbox.
5. Dispatcher tickea pero no envía nada (`PUBLISHING_DRY_RUN=true`).

Las páginas reales viven, así que cada ciclo descubre productos distintos. Si Amazon cambia el DOM, el `dom_healer` (Fase 4.2) entra en acción.

### Estado consolidado

| Fase | Estado |
|---|---|
| 1 — Auditoría + diseño + schema | ✅ |
| 2 — Core | ✅ |
| 3.1 — outbox_dispatcher + Evolution | ✅ |
| 3.2 — telegram_listener Telethon | ✅ |
| 3.3 — amazon_hunter + revalidator | ✅ |
| 3.4 — mercadolibre_hunter + afiliados | ✅ |
| 4 — watchdog + dom_healer + memory + deploy | ✅ |
| 5 — Orchestrator real | ✅ |
| **6 — Descubrimiento autónomo + credenciales legacy** | **✅** |

**317 tests verde. Cero regresiones. Cero diagnostics.**


---

## 2026-05-25 — Fase 7 (scheduler nocturno + warmup) ✅

El bot ahora respeta horarios humanos. Tres modos según hora local México:

| Modo | Ventana | Comportamiento |
|---|---|---|
| **`hibernating`** | 23:30 – 06:30 | Hunters/discovery dormidos. Dispatcher pausado (no publica nada, ni siquiera errores de precio). Watchdog y maintenance siguen activos. |
| **`warmup`** | 06:30 – 07:00 | Hunters/discovery activos a velocidad alta (90s en lugar de 600s). Outbox se llena. **Dispatcher sigue pausado** para acumular ofertas frescas. |
| **`active`** | 07:00 – 23:30 | Operación normal: hunters cada 600s, dispatcher publica respetando cooldown. |

A las 7am el operador ya tiene un outbox con ofertas detectadas durante el warmup, listas para publicar.

### Archivos creados

- `src/ofertas_hunter/runtime/scheduler.py` — `OperatingScheduler` + `ScheduleConfig` + `ModeDecision`. Ventanas configurables por env. Soporta cruce de medianoche (`23:30 → 06:30`). Clock inyectable. Cae limpio a UTC si `tzdata` no está. Fallback a `ACTIVE` si `enabled=false` (operación legacy).
- `tests/unit/runtime/test_scheduler.py` — 23 tests.

### Archivos modificados

- `src/ofertas_hunter/dispatching/dispatcher.py` — `OutboxDispatcher` acepta `scheduler` opcional. En `tick()`, si el modo es `hibernating` o `warmup`, no toca outbox (no publica). Loguea el cambio de modo una sola vez.
- `tests/unit/dispatching/test_dispatcher.py` — 4 tests nuevos verifican comportamiento en cada modo.
- `src/ofertas_hunter/orchestrator.py`:
  - `AgentFactoryBuilder` instancia un `OperatingScheduler` único compartido.
  - `_loop` recibe `scheduler`, `warmup_interval`, `hibernation_check_interval`. En hibernación NO llama a `work()`, sólo heartbeat cada 60s.
  - `make_dispatcher_factory` pasa el scheduler al dispatcher.
  - Hunters Amazon y ML usan el scheduler con `warmup_interval=90s`.
- `src/ofertas_hunter/config.py` — 8 vars nuevas: `schedule_enabled`, `schedule_timezone`, `hibernate_start/end`, `warmup_start`, `active_start`, `warmup_loop_interval_seconds`, `hibernation_check_interval_seconds`. Más `mercadolibre_cookies_fallback_path`.
- `src/ofertas_hunter/session/mercadolibre_session.py` — `from_settings` acepta `fallback_path` y lo usa si el primary no existe. Útil para reutilizar las cookies que el operador puso en `secrets/mercadolibre_cookies.example.json`.
- `src/ofertas_hunter/__main__.py`:
  - `cmd_status` ahora muestra modo actual, hora local y próximo cambio.
  - `cmd_run` propaga los flags del scheduler al `OrchestratorConfig`.
- `.env` y `.env.example` — secciones de scheduler nuevas.
- `requirements.txt` y `pyproject.toml` — añadido `tzdata==2026.2` (necesario en Windows para `zoneinfo` con `America/Mexico_City`).

### Tests añadidos (27, todos verde)

`tests/unit/runtime/test_scheduler.py` (23):
- `_within` con rangos normales y cruce de medianoche.
- `_delta_until` hoy/mañana.
- Modo correcto a 00:00, 02:30, 06:00, 06:29, 06:30, 06:45, 06:59, 07:00, 12:00, 20:00, 23:29, 23:30, 23:45.
- `next_change_in` correcto en cada modo.
- `enabled=False` siempre devuelve `ACTIVE`.
- Ventanas custom (22:00→05:00 hibernación, 05:00→06:00 warmup).

`tests/unit/dispatching/test_dispatcher.py` (4 nuevos):
- `test_dispatcher_hibernates_no_publishing` — outbox intacto en hibernación.
- `test_dispatcher_warmup_no_publishing` — outbox intacto en warmup.
- `test_dispatcher_active_publishes_normally`.
- `test_dispatcher_no_scheduler_means_always_active` — comportamiento legacy.

### Suite final

```
pytest -q
344 passed in 3.55s
```

### Verificación E2E del status

```
$ python -m ofertas_hunter status
  ...
  schedule:           active
  local_time (America/Mexico_City): 18:09:46
  next_change:        hibernating en 5:20:13
```

A las 23:30 cambia a `hibernating`. A las 06:30 a `warmup`. A las 07:00 a `active`.

### Cookies del legacy

El operador dejó cookies reales en `secrets/mercadolibre_cookies.example.json`. La nueva config (`MERCADOLIBRE_COOKIES_FALLBACK_PATH=secrets/mercadolibre_cookies.example.json`) hace que `MercadoLibreSession.from_settings` las cargue automáticamente cuando el primary `secrets/mercadolibre_cookies.json` no exista. Cuando el operador quiera la separación limpia, basta con renombrar `mercadolibre_cookies.example.json` → `mercadolibre_cookies.json`.

### Comportamiento durante hibernación

- Discovery: skip de la pasada. El frontier no se llena, no se quema cookies.
- Hunters Amazon/ML: skip. Playwright sigue arriba pero sin fetchear (puede dormir incluso sin abrir browser).
- Dispatcher: skip. Outbox no se mueve.
- Telegram listener: **sigue activo** — los canales mandan errores de precio en cualquier hora y queremos capturarlos para validarlos en warmup.
- Maintenance: sigue activo cada 6h.
- Watchdog: sigue activo monitoreando heartbeats.

### Comportamiento durante warmup (06:30 – 07:00)

- Discovery + hunters: intervalo 90s en lugar de 600s. Procesan más URLs por minuto.
- Dispatcher: pausado igual que en hibernación. Outbox crece sin publicar.
- A las 07:00 el dispatcher despierta y empieza a sacar ofertas con el primer cooldown (5 min entre normales).

### Estado consolidado

| Fase | Estado |
|---|---|
| 1 — Auditoría + diseño | ✅ |
| 2 — Core | ✅ |
| 3.1 a 3.4 — Marketplaces, dispatcher, telegram | ✅ |
| 4 — Watchdog + dom_healer + memory + deploy | ✅ |
| 5 — Orchestrator real | ✅ |
| 6 — Descubrimiento autónomo + credenciales | ✅ |
| **7 — Scheduler nocturno + warmup** | **✅** |

**344 tests verde. Cero regresiones desde Fase 1. Cero diagnostics.**

`PUBLISHING_DRY_RUN=true`, `PUBLISHING_ENABLED=false`, `TELEGRAM_ENABLED=false` siguen como defaults seguros. `SCHEDULE_ENABLED=true` por default.


---

## 2026-05-25 — Fase 8: kiro-cli como orquestador externo (MCP server)

Spec: `.kiro/specs/kiro-cli-orchestrator/`. Implementación completa siguiendo
el plan de tasks aprobado. **477 tests verde** (344 baseline + 133 nuevos),
**cero diagnostics**.

### Decisión de arquitectura

`ofertas_hunter` expone un **servidor MCP** (Model Context Protocol) por stdio
mediante el subcomando `python -m ofertas_hunter mcp-serve`. `kiro-cli`
(Claude Sonnet 4.6) actúa como cliente MCP y orquesta el ciclo del bot
invocando 16 tools de alto nivel. El comando autónomo
`python -m ofertas_hunter run` queda intacto como fallback.

**Hard_Rules permanecen en Python**: scoring, parsing, cooldown 5 min, gates
imagen+precio+URL, ML afiliado obligatorio, scheduler nocturno y filtro
Telegram→ML se aplican server-side. Argumentos del cliente con `force`,
`bypass_*` o `override_*` fallan con `validation_failed` por
`additionalProperties: false`.

### Tools MCP expuestas (16 totales)

**Lectura (5)** — sin efectos secundarios, autoApprove en config:
- `get_status` · `get_schedule_mode` · `get_outbox` · `get_recent_events` · `get_frontier_stats`

**Acción (7)** — respetan scheduler/cooldown/pausas:
- `discover_seeds` · `hunt_amazon` · `hunt_mercadolibre` · `dispatch_outbox`
- `revalidate_offer` · `pause_marketplace` · `unpause_marketplace`

**Quality gate (4)** — patrón request → submit con `review_token`:
- `request_offer_review` / `submit_offer_review` (approve/reject/rewrite_message)
- `improve_message_copy` / `submit_message_copy`

### Archivos nuevos en `src/ofertas_hunter/mcp/`

- `__init__.py` — exports lazy.
- `lockfile.py` — `FileLock` cooperativo cross-platform (Windows + POSIX) para
  detectar concurrencia entre `run` y `mcp-serve`.
- `serializers.py` — convierte `OutboxItem`, `Offer`, `RuntimeEvent`,
  `ModeDecision`, `PublishOutcome`, `RevalidationDetail` a dicts JSON-safe.
- `audit.py` — `audit_before/after/error` con sanitización (cookies, api keys,
  tokens, passwords redactados; strings >500 chars truncados).
- `safety.py` — 7 reglas: `schedule_authority`, `marketplace_paused`,
  `cooldown_normal`, `publishing_safe_mode`, `image_price_url`, `ml_affiliate`,
  `telegram_to_ml`. `apply_hard_rules` itera y devuelve la primera que rechaza.
- `context.py` — `ServerContext` con singletons lazy (browser, evolution
  client, publisher, dispatcher, hunters, discovery, revalidator), locks por
  marketplace, `pause_state`, `review_tokens`, `last_normal_publication_at`.
- `server.py` — `MCPServer` con `dispatch(name, args)` que ejecuta el pipeline:
  validate schema → audit_before → apply_hard_rules → handler → audit_after/error.
  Lazy import del SDK `mcp` para no exigirlo en otros comandos.
- `tools/__init__.py` — `ToolSpec` + `build_tool_registry`.
- `tools/read_tools.py` · `tools/action_tools.py` · `tools/quality_tools.py`.

### Cambios aditivos en código existente

- `src/ofertas_hunter/publishing/whatsapp_publisher.py`: el formatter respeta
  `payload["caption_override"]` cuando está presente. Default sin override =
  comportamiento idéntico al previo.
- `src/ofertas_hunter/__main__.py`: subcomando `mcp-serve` con `--no-lock`
  (escape para tests). Lockfile cooperativo en `data/mcp_serve.lock`.
- `pyproject.toml` + `requirements.txt`: añade `mcp==1.27.1` y
  `jsonschema==4.22.0`. Bump compatible: `httpx 0.27.0→0.27.2`,
  `pydantic 2.7.1→2.11.7`, `pydantic-settings 2.2.1→2.5.2` (todos los 344
  tests baseline siguen verde).

### Steering + docs operativas

- `.kiro/steering/ofertas-hunter-mcp.md` con `inclusion: fileMatch` y
  `fileMatchPattern: "*"`. Documenta objetivo, Hard_Rules con todos los
  tokens, ciclo recomendado, cuándo NO insistir, interpretación de
  runtime_events, anti-patterns del legacy `AmazonScrapperIA`, y
  consideraciones para subagentes (paralelización por marketplace).
- `docs/MCP_CLIENT_SETUP.md` con fragmento JSON listo para
  `~/.kiro/settings/mcp.json` (Windows + Linux/macOS), notas operativas y
  troubleshooting.

### Seeds actualizadas

- `config/seeds/amazon.json`: 36 → **141 URLs** (rankings + bestsellers +
  long-tail desde `amazon_mexico_seed_urls_y_productos.md`).
- `config/seeds/mercadolibre.json`: 36 → **106 URLs** (categorías + búsquedas
  específicas desde `mercadolibre_mexico_seed_urls_y_productos.md`).

### Tests nuevos (133)

- `tests/unit/mcp/test_dependency_smoke.py` (3) — imports de `mcp.server` y
  `mcp.types.Tool`.
- `tests/unit/mcp/test_lockfile.py` (8) — acquire / release / stale / conflict /
  re-acquire idempotente.
- `tests/unit/mcp/test_serializers.py` (8) — round-trip de OutboxItem, decimales,
  ISO Z, schedule decision.
- `tests/unit/mcp/test_audit.py` (8) — sanitize secrets, truncado, audit
  before/after/error con runtime_events.
- `tests/unit/mcp/test_safety.py` (22) — 7 reglas cubiertas con escenarios
  positivos y negativos + orden estable.
- `tests/unit/mcp/test_context.py` (9) — singletons (evolution, publisher,
  dispatcher, outbox), locks por marketplace, aclose idempotente.
- `tests/unit/mcp/test_server_dispatch.py` (8) — pipeline completo
  (unknown / validation / handler exception / safety skip / success /
  list returned wrapped).
- `tests/unit/mcp/test_read_tools.py` (15) — 5 read tools + filtros + smoke.
- `tests/unit/mcp/test_action_tools.py` (19) — 7 action tools + skip durante
  hibernating/warmup/paused + rechazo de extra args.
- `tests/unit/mcp/test_quality_tools.py` (16) — request/submit round-trip,
  preservación de campos materiales en rewrite, validación de tokens.
- `tests/unit/mcp/test_cli_mcp_serve.py` (3) — subparser registrado, exit 2 en
  conflicto de lockfile, `--no-lock` salta acquire.
- `tests/unit/mcp/test_steering_workspace_scope.py` (6) — frontmatter
  `inclusion: fileMatch`, secciones requeridas, tokens de Hard_Rules
  documentados, anti-patterns del legacy, doc MCP_CLIENT_SETUP completa.
- `tests/integration/mcp/test_mcp_smoke_in_process_client.py` (8) — handshake
  anuncia las 16 tools, descriptores con `additionalProperties: false`, cada
  read/action/quality dispatcheable, audit emite before+after.

### Anti-patterns que evitamos del legacy `AmazonScrapperIA`

- **Una tool por producto** → Las acciones son por marketplace y por
  agregado (`hunt_amazon(limit=5)`, no `process_url(url)`). El legacy gastaba
  tokens por cada item.
- **Scoring/parsing en el LLM** → Permanecen 100% en Python.
- **Override de Hard_Rules por argumento** → `additionalProperties: false`
  en todos los schemas; las reglas no consultan args para decidir.
- **Steering duplicando lógica determinista** → El doc describe contratos y
  tokens, no thresholds numéricos.

### Verificación final

```
$ pytest -q
477 passed in 8.06s

$ getDiagnostics src/ofertas_hunter/mcp/* src/ofertas_hunter/__main__.py \
                  src/ofertas_hunter/publishing/whatsapp_publisher.py
No diagnostics found

$ python -m ofertas_hunter mcp-serve --help
usage: ofertas-hunter mcp-serve [-h] [--no-lock]
options:
  -h, --help  show this help message and exit
  --no-lock   No tomar el lockfile (usar SOLO en tests in-process).
```

### Próximos pasos (no bloqueantes)

1. Configurar `~/.kiro/settings/mcp.json` siguiendo `docs/MCP_CLIENT_SETUP.md`.
2. Probar el ciclo completo desde `kiro-cli` con `get_status` → `discover_seeds`
   → `hunt_amazon` (Amazon CAPTCHA es real, monitorea `runtime_events`).
3. Para producción: `PUBLISHING_ENABLED=true` y `PUBLISHING_DRY_RUN=false` en
   `.env` cuando el operador lo decida (no antes).


---

## 2026-05-26 — Fix: falsos positivos de error de precio (accesorios genéricos en ML)

### Problema

3 cargadores 20W "compatible con iPhone" en Mercado Libre publicados como
**🚨 ERROR DE PRECIO 🚨** con `confidence_label=medium` (outbox ids 44 / 45 /
46, precios $76.62 / $93.98 / $160.05). Ninguno es un error de precio: son
accesorios genéricos cuyo rango normal es $50–$300 MXN.

### Causa raíz

- `_fingerprint_smartphone` en `price_error_scorer.py` aceptaba cualquier
  título con la palabra "iphone".
- Combinado con la regla `smartphone_below_500_extreme[+35]`, los cargadores
  llegaban a score 60+ → `possible_price_error` → outbox `price_error` con
  bypass cooldown.
- `format_price_error` permitía `confidence_label="medium"`.
- El publisher no tenía guardrail para PE de confianza media.

### Cambios

#### Detección

- Nuevo módulo `intelligence/accessory_detector.py`:
  - `assess_title()` devuelve `AccessoryAssessment` con flags
    `is_generic_accessory`, `mentions_compatible_with_premium`,
    `is_real_premium_product`, `category_guess`, `matched_tokens`.
  - Tokens primarios: cargador, cable, adaptador, protector, mica, funda,
    case, carcasa, soporte, vidrio templado, etc. (matching con word
    boundaries para no chocar con "carcasa" dentro de palabras).
  - Patterns de "compatible con iPhone/Samsung/iPad", "para iPhone",
    listas tipo "iPhone 16/15/14/13/12".

#### Scorer (REGLAS 2-3-5)

- `PriceErrorScorer.score()` calcula assessment una sola vez al inicio.
- `premium_brand_counts` = premium real (no compatible y no accesorio).
- Bonus de smartphone / tablet / audio / laptop bloqueados cuando
  `is_generic_accessory=True`.
- Penalty `-45` por accesorio genérico, reason
  `generic_accessory_not_price_error`.
- Penalty `-30` por "compatible con premium" sin producto premium real,
  reason `compatible_with_premium_brand_not_premium_product`.
- Cap: si es accesorio genérico, score máximo `55`
  (`suspicious_deal`), salvo histórico propio extremo (caída ≥85%) sobre
  producto premium real.
- Confianza máxima `medium` para accesorios genéricos (REGLA 5).

#### Publisher (REGLA 6-7)

- `WhatsAppPublisher._medium_pe_guardrail()`: bloquea cualquier
  `OutboxType.PRICE_ERROR` / `POSSIBLE_PE` cuyo `confidence_label` no sea
  high/very high. Si `discount_percent>=50` continúa para degradar; si no,
  devuelve `discard_reason=discarded_false_price_error_medium_confidence`.
- `WhatsAppPublisher._maybe_degrade_item()`: re-tipifica el item a
  `normal` y deriva `previous_price` si falta.
- `PublishOutcome.discard_reason`, `degraded_outbox_type`,
  `degraded_payload` añadidos.
- `formatter.format_price_error` rechaza `confidence_label != high|very high`.

#### Dispatcher

- `OutboxDispatcher._publish_item()` ahora respeta `discard_reason`
  (marca `outbox.state=discarded`) y `degraded_outbox_type` (actualiza
  type+payload antes de marcar `sent`).

#### CLI (REGLA 9)

- `python -m ofertas_hunter audit-false-price-errors [--fix] [--days N]`:
  recorre outbox con `type=price_error/possible_pe`, detecta accesorios
  genéricos / "compatible con", emite
  `runtime_event(kind=false_price_error_detected)` y, con `--fix`,
  degrada o descarta items pending.

#### Memoria negativa (REGLA 10)

- `data/training/false_price_errors/generic_chargers.jsonl` con los 3
  ejemplos como dataset negativo permanente.

#### Tests

- `tests/unit/intelligence/test_false_price_errors_accessories.py`:
  - los 3 falsos positivos NO se clasifican como price_error_confirmed.
  - reason incluye ambos penalties.
  - el formatter rechaza medium.
  - el dispatcher descarta medium-PE sin descuento.
  - "compatible con iPhone/Samsung" no cuenta como producto Apple/Samsung.
  - los PE reales (iPhone 16 Pro Max, Galaxy S24, AirPods Pro $599,
    laptop combo $305) siguen clasificándose como `price_error_confirmed`
    con `very high`/`high`.
- `tests/unit/intelligence/test_false_price_error_audit.py`:
  - audit detecta sin `--fix`.
  - `--fix` descarta sin descuento.
  - `--fix` degrada con `discount_percent>=50`.
  - audit no toca PE reales premium.
- 513 / 513 unit tests passing.


---

## 2026-05-26 — Fix: falsos positivos de CAPTCHA Amazon + port anti-detección legacy

### Problema reportado

Amazon era pausado por "Captcha detectado" en muchas URLs y el operador
sospechaba falsos positivos. El proyecto legacy `AmazonScrapperIA`
funcionaba sin esos errores.

### Auditoría

- 90/97 heal_samples del legacy son captchas reales (legacy SÍ veía captcha).
- 42/43 snapshots Amazon en la DB del nuevo proyecto son captchas reales
  (form action `/errors/validateCaptcha` + visible "Continuar a Compras").
- 1/43 era un timeout con HTML vacío que el detector ingenuo confundió.
- El detector previo usaba matching ingenuo por substring sobre HTML raw,
  vulnerable a futuros bundles JS que mencionen `validateCaptcha` o
  `amzn-captcha`.

### Causa raíz

`PlaywrightBrowserWorker._is_captcha_page` matcheaba 4 substrings sobre
HTML raw, sin distinguir estructura visible vs scripts/JSON/comentarios.
Una sola entrada con `length(content)=0` (timeout) fue clasificada como
captcha y disparó la pausa de marketplace.

### Cambios

#### Detector

- Nuevo `browser/amazon_captcha_detector.py` con
  `AmazonCaptchaDetector.assess(html, final_url, status)` que devuelve
  `CaptchaAssessment` con `is_captcha`, `confidence` (high/medium/low/none),
  `strong_signals`, `weak_signals`, `visible_signals`,
  `should_pause_marketplace`, `reasons`.
- Señales fuertes: `<form action="/errors/validateCaptcha">`,
  `<input id|name="captchacharacters">`, `<input name="amzn-captcha…">`,
  `<img src=".../captcha/...">`, URL final con `/errors/validateCaptcha`.
- Señales visibles: title `Robot Check`, "Continuar a Compras",
  "Lo sentimos, parece que está utilizando un programa automatizado",
  "Enter the characters you see below", etc.
- Señales débiles (substrings): `validateCaptcha`, `amzn-captcha`,
  `captcha-instrumentation` por sí solas → `confidence=low` y
  `should_pause_marketplace=False`.

#### Browser worker

- `PlaywrightBrowserWorker` aplica el detector estructural sólo a URLs
  Amazon (ML conserva matching simple).
- Sólo marca `blocked=True` si confidence=high. Medium/low se pasan al
  agente vía `RenderedPage.extras["captcha_assessment"]`.
- Anti-detección portada del legacy:
  - Args Chromium completos (`--no-sandbox`, `--disable-extensions`,
    `--no-first-run`, `--disable-default-apps`, `--disable-infobars`,
    `--window-size`, `--start-maximized`, `--user-agent`).
  - `extra_http_headers` legítimos (`Accept-Language`, `Sec-Ch-Ua`,
    `Sec-Fetch-*`, `Upgrade-Insecure-Requests`, etc.).
  - Warmup opcional (`BrowserConfig.warmup_amazon_homepage`) que visita
    la homepage para establecer cookies anónimas.
  - Retry automático 503 para URLs Amazon (15–30s, una vez).

#### Agente

- `AmazonHunterAgent` distingue 3 ramas en fetch fallido:
  - high → `amazon_captcha_confirmed` (severity=error), reason
    `captcha_detected`. Se permite a la lógica de pausa actuar.
  - medium → `amazon_suspected_false_captcha` (severity=warning),
    reason `amazon_extraction_failed`. NO suma a pausa.
  - low → `amazon_suspected_false_captcha`, reason
    `amazon_possible_block_low_confidence`. NO suma a pausa.

#### CLI

- `python -m ofertas_hunter audit-amazon-captcha [--recent] [--fix] [--days N]`:
  recorre `dom_snapshots` Amazon, reclasifica con el detector nuevo,
  emite `runtime_event(kind="amazon_captcha_audit_kept" |
  "amazon_captcha_false_positive_reclassified")`. Con `--fix` actualiza
  los `discarded_candidates` afectados.
- `python -m ofertas_hunter amazon-captcha-check <url|path>`: ejecuta el
  detector contra una URL en vivo (Playwright) o un snapshot HTML local.

#### Tests

- `tests/unit/browser/test_amazon_captcha_detector.py` (22 tests):
  cubre los 5 escenarios reales, los 4 falsos positivos (script, JSON,
  telemetry, página de producto), 503/timeout/selector miss/precio
  faltante, 4 reglas de pausa, e integración contra los 90 heal_samples
  reales del legacy + debug_product.html y debug_search.html.
- `tests/unit/agents/test_amazon_hunter_agent.py`: 3 tests nuevos para
  los 3 caminos del agente (confirmed, low, medium suspect).

#### Documentación

- `docs/AMAZON_LEGACY_CAPTCHA_AUDIT.md` con la auditoría completa
  legacy vs nuevo, decisiones, reglas, tests y comandos de verificación.

### Resultados

- 538/538 unit tests passing (was 513).
- audit-amazon-captcha sobre la DB real: 42 captchas reales kept, 1
  reclassified, 1 discarded_candidate actualizado.
- Mercado Libre, Telegram, dispatcher, ML afiliados, scoring de errores
  de precio: intactos.


---

## 2026-05-26 — Sesión persistente del navegador (login manual)

### Necesidad

El operador quería iniciar sesión manualmente una vez (Amazon, Mercado
Libre, etc.) y que el bot reutilizara la sesión entre runs sin tener
que exportar cookies a JSON.

### Cambios

- `BrowserConfig.user_data_dir`: nuevo campo. Cuando está set, el worker
  usa `playwright.chromium.launch_persistent_context()` apuntando a
  ese directorio. Cookies, localStorage, sessionStorage, indexedDB y
  service workers persisten entre runs.
- `Settings.amazon_user_data_dir` (default `secrets/browser_profiles/amazon`)
  y `Settings.mercadolibre_user_data_dir` (default
  `secrets/browser_profiles/mercadolibre`).
- `Orchestrator` pasa esos paths a los `BrowserConfig` de Amazon y ML
  hunters automáticamente.
- Nuevo CLI:

  ```
  python -m ofertas_hunter login --marketplace mercadolibre
  python -m ofertas_hunter login --marketplace amazon
  python -m ofertas_hunter login --url https://example.com --profile-name mi_perfil
  ```

  Abre Chromium NO headless con el perfil persistente, navega a la
  URL inicial (homepage del marketplace) y espera ENTER en la
  terminal antes de cerrar. Resource blocking y screenshot-on-failure
  están deshabilitados durante el login para que la página cargue
  todo (captcha, fuentes, imágenes).
- `.gitignore`: `secrets/browser_profiles/` añadido para no commitear
  la sesión.
- `.env.example`: documenta `AMAZON_USER_DATA_DIR`,
  `MERCADOLIBRE_USER_DATA_DIR` y `AMAZON_WARMUP_HOMEPAGE`.

### Compatibilidad

- ML sigue cargando cookies del JSON cuando existen. La sesión
  persistente es complementaria, no la reemplaza.
- Telegram (Telethon) ya tenía su propia sesión vía `secrets/telegram.session`
  — sin cambios.
- 538/538 tests passing.


---

## 2026-05-26 — Iter 2: opción 3 del lanzador no usaba sesión persistente Amazon

### Síntoma

Tras la iteración 1 (detector estructural), el operador siguió viendo
en `start.ps1 → [3] Modo autónomo Python` (que ejecuta
`scripts/orquestador_ia.py`):

```
Captcha confirmado en https://www.amazon.com.mx/dp/B00DGQMJE0 (signals=['form_action_validate_captcha'])
[Amazon] ciclo #1 procesados=5 encoladas=0
[WARN] Amazon: 5 CAPTCHAs — pausando 10 min
```

### Causas raíz

1. **MCP browser desnudo**: `ServerContext.get_browser()` ignoraba
   `user_data_dir`. La opción 3 (orquestador_ia → MCP → AmazonHunterAgent)
   no usaba la sesión persistente. Amazon servía captchas reales con
   alta frecuencia.
2. **`loop_amazon` sumaba string equality**: contaba todos los
   `discarded_reason == "captcha_detected"` sin leer `confidence` ni
   `should_pause_marketplace`.

### Cambios

- `mcp/context.py`: tres browsers separados (`_browser`, `_amazon_browser`,
  `_ml_browser`). Cada hunter usa el suyo con su `user_data_dir`.
- `agents/amazon_hunter_agent.py`:
  - `HuntOutcome` con 6 campos de captcha
    (`captcha_confidence`, `captcha_should_pause_marketplace`, etc.).
  - Sospecha medium/low ya no parsea como producto.
  - `_save_captcha_debug()` persiste HTML + screenshot + metadatos en
    `data/debug/amazon_captcha/`.
  - Emite `runtime_event(amazon_suspected_captcha_form_not_visible)` para
    form_action aislado.
- `mcp/tools/action_tools.py`: `_summarize_outcome` expone los 6 campos.
- `browser/amazon_captcha_detector.py`: `high` ahora exige estructura
  fuerte + visible (no `body_tiny_with_title` como sustituto).
- `browser/playwright_worker.py`: log de captcha incluye `strong`,
  `visible`, `confidence`, `should_pause`.
- `scripts/orquestador_ia.py`: pausa 10min sólo con >=2 captchas REALES
  high-confidence; 1 → backoff corto 60s; sospechosos sin should_pause
  no cuentan.

### Tests

`tests/unit/agents/test_amazon_captcha_pause_policy.py` (13 tests):
form_action solo no pausa, outcome lleva el assessment, debug snapshot
guardado, summarize_outcome expone campos, política de pausa con 0/1/2+
captchas, sin detectores duplicados, fixtures legacy AmazonScrapperIA
no se marcan, CLI reporta should_pause=False para medium, opción 3 usa
detector central.

Resultado: **551/551** tests passing (was 538).

### Verificación contra URLs del log del operador

```
amazon-captcha-check https://www.amazon.com.mx/dp/B00DGQMJE0
→ strong=['form_action_validate_captcha']
  visible=['text_continuar_comprando']
  confidence=high should_pause=True
```

Las 5 URLs son captchas reales. Con el fix la opción 3 ya no pausa por
1 captcha aislado ni por form_action sin visible.

### Recomendación operador

Para reducir captchas reales en Amazon, además de la sesión persistente
ML que ya creó, hacer también:

```
python -m ofertas_hunter login --marketplace amazon
```

Eso establece cookies anónimas legítimas en
`secrets/browser_profiles/amazon/` y reduce drásticamente la frecuencia
con que Amazon sirve captchas a la sesión.


---

## 2026-05-26 — Iter 3: replicar fielmente el flujo del legacy AmazonScrapperIA

### Síntoma persistente

Después de iter 1 y 2, en `start.ps1 [3]` aún aparecían 1-5 captchas
reales por ciclo. El operador insistió en que el legacy
`AmazonScrapperIA` (también nuevo, 2 días) corría sin captchas en
background. Comparación línea a línea reveló diferencias críticas que no
se habían portado.

### Diferencias clave detectadas

1. **Tab por fetch vs tab persistente**. El nuevo abría
   `context.new_page()` por cada URL y la cerraba al final. El legacy
   navega URL tras URL en la **misma pestaña** durante toda la sesión.
   Abrir/cerrar pestañas es señal fuerte de automation.
2. **Sin pausa post-goto**. El legacy hace `await asyncio.sleep(0.8-1.5s)`
   tras cada `goto` exitoso. El nuevo iba directo a `page.content()`.
3. **Bloqueo de recursos `media`/`font`**. El nuevo usa `route.abort()`
   para acelerar; el legacy carga todo. Eso cambia el patrón de
   network requests, otra señal de bot.
4. **Sin backoff post-captcha**. El nuevo procesaba URL siguiente sin
   esperar; el legacy hace `_handle_captcha` con backoff exponencial
   `30s → 60s → 120s → 300s`.

### Cambios aplicados

#### `browser/playwright_worker.py`

- `self._page` field: pestaña persistente reusada entre fetches.
- `_get_or_create_page()`: la crea sólo si no existe o si está cerrada.
- `_do_fetch()` ya no llama `page.close()` — la pestaña vive todo el run.
- Sólo se descarta si Playwright reporta `is_closed()` tras un error.
- Pausa post-goto:
  - 0.8-1.5s en status 200
  - 0.5-1.0s en redirects 301/302
- Backoff post-captcha (`_consecutive_amazon_captchas`,
  `_captcha_backoff_until`): tras un `blocked=True`, el siguiente
  `fetch()` espera `30 * 2^(n-1)` segundos (cap 300s). Reset en éxito.

#### `browser/browser_context.py`

- `block_resource_types` default cambiado de `("media", "font")` a `()`.
  Carga de recursos completa replica el patrón humano del legacy.

### Verificación

`python -m ofertas_hunter run --once --limit 8`:

```
amazon discovery: 181 seeds añadidas al frontier
Amazon warmup OK (homepage cookies set)
amazon_hunter fetch B000HCRVUS
amazon_hunter fetch B07DHDFW5V
... (8 URLs)
amazon hunt: procesados=8 encolados=0 descartados=8
ml hunt: procesados=...
```

**Cero líneas `Captcha confirmado`.** Cero pausas. Procesamiento
completo de las 8 URLs, exactamente como el legacy.

### Tests

551/551 unit tests passing (sin cambios netos). Los tests existentes
validan:
- detector estructural sigue clasificando captchas reales como `high`.
- `should_pause_marketplace=False` para form_action sin visible.
- política de pausa requiere ≥2 captchas reales en una ronda.
- runtime_event `amazon_captcha_confirmed` sólo se emite con
  `confidence=high`.

### Estado final del bot

Ya está alineado con el legacy AmazonScrapperIA en todos los aspectos
relevantes de anti-detección:

- Misma pestaña reutilizada entre URLs.
- Warmup de homepage al inicio.
- Headers HTTP completos del legacy.
- Stealth args completos.
- `--enable-automation` removido.
- Pausa post-goto + scroll humano + mouse jitter.
- Carga completa de recursos (no aborto).
- Sesión persistente con `user_data_dir`.
- Backoff exponencial post-captcha.
- Detector estructural propio (mejora sobre el legacy).
- Política de pausa basada en `confidence=high` (mejora).


---

## 2026-05-26 — Fix: caption_override sólo se acepta si respeta formato canónico

### Síntoma

Algunas ofertas se publicaban en WhatsApp con un formato distinto al
canónico (sin asteriscos en `*X% de descuento*`, sin tildes `~$..~` en
`Antes:`, líneas extra de bullets de features). El usuario recibía
publicaciones inconsistentes.

### Causa raíz

El MCP quality gate (tool `improve_message_copy` + `submit_message_copy`)
permite que el orquestador IA reescriba el `caption_override` del
payload. El publisher honraba el override sin validar que respetase
el formato `*Título*` / `*X% de descuento*` / `❌ Antes: ~$N~` /
`✅ *AHORA: $M*` / `*Ver oferta:*`.

### Cambios

- `publishing/whatsapp_publisher.py`:
  - Nuevo helper `_caption_respects_canonical_format(text, item_type, payload)`
    que valida con regex la presencia de:
    - título en `*...*`
    - `*X% de descuento*`
    - `*AHORA: $...*`
    - `*Ver oferta:*`
    - `~$...~` cuando hay `previous_price`
    - URL del payload presente en el texto.
  - Si la validación falla, el publisher emite warning y cae al
    formato canónico (`format_normal_offer` / `format_price_error`).

- `scripts/fix_caption_overrides.py`: utilidad para limpiar overrides
  ya almacenados que no cumplen formato. Modo dry-run por defecto;
  con `--apply` borra el `caption_override` para que el dispatcher
  use formato canónico.

### Resultado

- Limpiados 12 items pending del outbox que tenían override sin
  formato canónico (la IA quitó asteriscos / tildes en su rewrite).
- 2 items conservados porque sí respetaban el formato.
- Próximas reescrituras de la IA tendrán que respetar la plantilla
  o serán ignoradas en publicación.

551/551 unit tests passing.
