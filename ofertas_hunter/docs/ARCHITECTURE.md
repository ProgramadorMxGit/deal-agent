# ofertas_hunter — Arquitectura

> Bot autónomo, persistente e inteligente para detectar ofertas reales >= 50% y
> errores de precio en Amazon México, Mercado Libre México y canales de Telegram,
> publicando ofertas aprobadas en un grupo de WhatsApp vía Evolution API.

## 1. Principios

- **Multi-fuente, single-canal-de-publicación**: muchos hunters → un dispatcher
  serializado a WhatsApp.
- **Persistente sobre todo**: SQLite con WAL como única verdad. JSON sólo para
  export/debug.
- **Auto-curativo**: cualquier agente puede caerse y el watchdog lo reinicia
  sin perder estado. El dom_healer puede actualizar selectores y aplicar
  patches sólo si los tests pasan.
- **No publica si no puede validar**: imagen, link y precio son obligatorios.
- **Errores de precio tienen prioridad máxima** y saltan cooldown.
- **Cero feedback humano obligatorio**: el bot autoaprende.
- **No quema cookies**: rate limits, jitter, backoff y degradación cuando
  detecta bloqueo.

## 2. Componentes

```
                       ┌──────────────────────────────────────┐
                       │            Orchestrator              │
                       │  (asyncio supervisor + límites)      │
                       └──────────┬───────────────────────────┘
                                  │
       ┌──────────────────────────┼──────────────────────────┐
       │                          │                          │
       ▼                          ▼                          ▼
┌────────────┐          ┌────────────────┐         ┌─────────────────┐
│ Hunters    │          │  Telegram      │         │  Self-healing   │
│ (paralelo) │          │  Listener      │         │  + Watchdog     │
└────────────┘          └────────────────┘         └─────────────────┘
   │   │   │                     │                          │
   │   │   ▼                     ▼                          ▼
   │   │ Mercado Libre     Channel events           Selector updates
   │   ▼ Hunter (Playwright + cookies)              + DOM snapshots
   ▼ Amazon Hunter
   (Playwright + stealth)
   │
   └───────► PriceIntelligence (descuento + score error de precio)
                          │
                ┌─────────┼──────────┬──────────────┐
                ▼         ▼          ▼              ▼
            publish    enqueue   watchlist     discard
             (E.P.)    (>=50%)   (suspicious)  (con razón)
                                            
                                          ┌─────────────┐
                                          │  Outbox     │  cooldown 5min global
                                          │  (SQLite)   │  random pick si hay
                                          │             │   muchos elegibles
                                          └─────┬───────┘  revalida si > 1h
                                                │
                                                ▼
                                       ┌───────────────────┐
                                       │ Outbox Dispatcher │
                                       │ (serializado)     │
                                       └─────┬─────────────┘
                                             │
                                             ▼
                                    Evolution API → WhatsApp
```

## 3. Capas

### 3.1 `core` (config, db, models, logging)

- **`config.py`**: pydantic-like loader que resuelve `.env` → JSON → defaults.
- **`db.py`**: pool SQLite con `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`,
  `foreign_keys=ON`, ContextManager async.
- **`models.py`**: dataclasses puros para Offer, PriceObservation, OutboxItem,
  RuntimeEvent, AgentRun, etc.
- **`logging_setup.py`**: rotating file + structured JSON logs.

### 3.2 `marketplaces`

Cada marketplace expone una clase `Hunter` con:

```python
class BaseHunter(Protocol):
    name: str
    async def start(self) -> None: ...
    async def explore(self) -> AsyncIterator[OfferCandidate]: ...
    async def close(self) -> None: ...
```

Implementaciones:
- `marketplaces/amazon/hunter.py` (de `AmazonScrapperIA/src/browser_worker.py`).
- `marketplaces/mercadolibre/hunter.py` (de `bot_diversidad_global/src/orchestrator.py`).
- Cada una con su `price_parser.py`, `product_parser.py`, `selectors.json`,
  `seeds.json`, `session_loader.py`.

### 3.3 `extraction`

- `image_resolver.py`: garantiza que toda oferta publicable tenga imagen.
  Estrategia: DOM image > OpenGraph meta > microdata > galería.
- `price_parser.py`: utilidades genéricas (parse_price_text MX/Europeo,
  detect_monthly_payment, normalize_currency).
- `url_resolver.py`: resuelve shortlinks (meli.la, amzn.to, bit.ly).

### 3.4 `intelligence`

- `discount_calculator.py`: calcula descuento real con histórico.
- `price_error_scorer.py`: scorer 0-100 determinista (ver
  PRICE_ERROR_DETECTION.md).
- `category_inferer.py`: deduce categoría desde título (ML básico).
- `llm_evaluator.py` (opcional): wrapper kiro-cli/Anthropic para criterios
  cualitativos.
- `historical_anomaly.py`: compara precio actual vs histórico propio
  + categoría.

### 3.5 `publishing`

- `formatter.py`: dos templates (`oferta_normal`, `error_de_precio`) según
  el formato exacto especificado en RULES.md.
- `whatsapp_evolution.py`: cliente HTTP a Evolution API
  (`/message/sendText`, `/message/sendMedia`).
- `image_downloader.py`: descarga + valida imagen + base64 encoding para
  Evolution.

### 3.6 `dispatching`

- `outbox.py`: cola persistente SQLite. Métodos: `enqueue`, `pick_random_eligible`,
  `mark_published`, `mark_failed`, `revalidate_if_old`.
- `dispatcher.py`: loop serializado que respeta cooldown global, hace
  bypass para errores de precio, revalida ofertas > 1h.
- `cooldown.py`: 5min normal, 0 para errores, configurable.

### 3.7 `telegram`

- `listener.py`: Telethon que escucha canales configurados (los que ya
  están en MEMORY.md de legacy: ofertonesmexico, OFERTAS PREMIUM MX,
  OFERTAS RELAMPAGO).
- `message_parser.py`: extrae tienda, título, precio, link, urgencia,
  imagen, categoría sugerida (patrones documentados en
  TELEGRAM_PRICE_ERROR_PATTERNS.md).
- `signal_extractor.py`: detecta términos de urgencia, mayúsculas
  excesivas, emojis 🚨🔥‼️.

### 3.8 `memory`

- `store.py`: tabla `memory_summaries` para resúmenes.
- `compressor.py`: compacta por tamaño + relevancia.
- `summary_writer.py`: genera resúmenes periódicos de:
  - patrones de precio por categoría
  - selectores inestables
  - razones frecuentes de descarte
  - señales de error de precio
  - tiendas con errores reales
  - rangos normales por marca/categoría

### 3.9 `self_healing`

- `dom_healer.py`: detecta degradación, captura snapshots, propone
  selectores (heurísticas primero, IA opcional), corre tests del fixture,
  aplica si pasan, revierte si fallan.
- `degradation_monitor.py`: deque por (marketplace, context).
- `selector_versioner.py`: tabla `selector_versions` con `applied_at`,
  `applied_by`, `reverted_at`.
- `self_repair.py`: aplicar patches de código sólo cuando los tests
  pasan; registro en `self_patches`.

### 3.10 `agents`

Cada agente es una clase con:

```python
class BaseAgent:
    name: str
    async def run(self) -> None: ...
    async def shutdown(self) -> None: ...
```

Ver `AGENTS.md` para la lista y responsabilidades.

## 4. Flujo de datos

```
candidate_detected
   │
   ▼
normalize_product           (extraction.product_normalizer)
   │
   ▼
resolve_url                 (extraction.url_resolver)
   │
   ▼
extract_price_image_stock   (marketplaces.<m>.product_parser)
   │
   ▼
save_price_observation      (db.price_observations)
   │
   ▼
evaluate_discount           (intelligence.discount_calculator)
   │
   ▼
evaluate_price_error        (intelligence.price_error_scorer)
   │
   ▼
decide:
   ├── price_error_confirmed (>=80) → publish_immediately
   ├── normal_offer (>=50%)         → enqueue_outbox
   ├── possible_price_error (60-79) → revalidate, then decide
   ├── suspicious_deal (40-59)      → save_watchlist
   └── invalid/noise                → discard with reason

before_publish:
   ├── validate image
   ├── validate price
   ├── validate stock
   ├── validate URL final
   └── if outbox age > 1h → revalidate with Playwright

publish_to_whatsapp
   │
   ▼
save published record
   │
   ▼
update memory
```

## 5. Concurrencia

- **Hunters** corren en paralelo con límites por agente
  (`max_concurrent_pages_per_hunter`).
- **Telegram listener** usa workers (3 por defecto) para parsear mensajes.
- **Outbox dispatcher** es estrictamente serializado (un solo task).
- **Errores de precio** tienen su propia cola de prioridad, también
  serializada.
- **Memory compressor** corre en intervalos largos (cada 6h).
- **Watchdog** monitorea cada 30s.

## 6. Persistencia (SQLite WAL)

Tablas (ver `migrations/001_init.sql`):

```
products
├─ id, marketplace, marketplace_id, title, brand, category,
│  category_inferred, condition, url_canonical, image_url,
│  first_seen_at, last_seen_at

price_observations
├─ id, product_id, current_price, previous_price, currency,
│  discount_percent, has_stock, source, raw_signals_json,
│  observed_at

offers
├─ id, product_id, current_price_observation_id,
│  classification, score, reasons_json, created_at,
│  state ('candidate'|'eligible'|'published'|'discarded'|'expired'|'watchlist')

outbox
├─ id, offer_id, type ('normal'|'price_error'|'possible_pe'),
│  enqueued_at, scheduled_for, attempts, last_attempt_at,
│  state ('pending'|'in_flight'|'sent'|'failed'|'discarded'),
│  message_payload_json

published_messages
├─ id, outbox_id, offer_id, sent_at, evolution_response,
│  message_text, media_url, success

discarded_candidates
├─ id, source ('amazon'|'meli'|'telegram'),
│  raw_payload_json, reason, created_at

visited_urls
├─ id, marketplace, url_canonical, visited_at

frontier
├─ id, marketplace, url_canonical, url_type, score,
│  added_at, retries

dom_snapshots
├─ id, marketplace, context, url, content, captured_at, reason

selector_versions
├─ id, marketplace, context, key, selector_value,
│  applied_at, applied_by, reverted_at, fixture_path,
│  test_pass

agent_runs
├─ id, agent_name, started_at, ended_at, status, summary_json

self_patches
├─ id, file_path, diff, applied_at, tests_passed,
│  reverted_at, reason

memory_summaries
├─ id, kind, content, generated_at

runtime_events
├─ id, kind, severity, payload_json, created_at,
│  acknowledged_at

telegram_messages
├─ id, channel, message_id, text, image_path,
│  original_url, resolved_url, captured_at, processed_at

price_error_examples
├─ id, source, payload_json, classification_expected,
│  notes

category_price_ranges
├─ id, category, brand, min_normal, max_normal,
│  median, sample_size, updated_at

product_aliases
├─ id, product_id, alias, source

resolved_urls
├─ id, original_url, final_url, resolved_at, http_status
```

Ver `docs/SCHEMA.md` para detalle completo (constraints, índices,
vistas).

## 7. Seguridad operativa

- Cookies de Mercado Libre persistentes con la misma lógica del legacy.
- User-agent rotation y viewport rotation por sesión.
- Rate limit por hostname (token bucket).
- Jitter en delays.
- Backoff exponencial en 503 / captcha.
- No reintenta agresivamente si una tienda bloquea.
- Toma de credenciales: `.env` > config files > `secrets/` (gitignored).

## 8. Despliegue

- Desarrollo: PowerShell + venv local + `python -m ofertas_hunter`.
- Producción: VPS Ubuntu, systemd, cookies compartidas vía path env.
- Docker Compose opcional (Evolution API + bot en mismo compose).

Ver `docs/DEPLOY.md`.

## 9. Aprendizaje autónomo

El bot mejora sin feedback humano:

- Tras publicar, monitorea si la oferta sigue viva 1h después.
- Si una oferta normal no se valida en revalidación, registra patrón.
- Si una oferta de Telegram con keyword "error de precio" termina siendo
  precio mensual o accesorio, reduce el peso de ese keyword.
- Tras N descartes con la misma razón, sube esa razón al ranking.
- Cada semana el `memory_compressor` produce summary que alimenta los
  scorers.

## 10. No-objetivos

- No es un agregador público.
- No paga afiliados (solo extrae el link cuando aplica para Mercado Libre).
- No reemplaza el legacy `ofertas-detector-vps` — esto es independiente
  y autónomo.
- No usa kiro-cli como dependencia obligatoria — es opcional para casos
  cualitativos.
