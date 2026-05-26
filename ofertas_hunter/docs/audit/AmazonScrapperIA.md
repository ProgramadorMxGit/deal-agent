# Auditoría — AmazonScrapperIA

> Fecha: 2026-05-25
> Alcance: `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\AmazonScrapperIA`
> Propósito: identificar piezas reutilizables para `ofertas_hunter`.

## 1. Resumen ejecutivo

`AmazonScrapperIA` es un scraper de Amazon México con Playwright + integración
`kiro-cli` (Claude Sonnet 4.5) como cerebro IA. Su valor diferencial respecto
a `bot_diversidad_global` está en:

- **DOM Healer** funcional con auto-actualización de `selectors.json`.
- **MemoryStore** con histórico de evaluaciones IA, seeds, categorías, ASINs.
- **AIEvaluator** con tres modos (kiro-cli, ANTHROPIC_API_KEY, reglas) y
  prompt batch.
- **Antibot agresivo**: stealth script, UA rotation, captcha detection,
  warmup, scroll humano, mouse movements.
- **MCP server** que expone `navigate_and_extract`, `generate_exploration_urls`,
  etc. — útil como integración futura con agentes Kiro.
- **Telegram sender** con formato exacto (foto + caption Markdown).
- **97 snapshots HTML** en `data/heal_samples/` para entrenar fixtures.

**Veredicto global:** la lógica de Amazon (price_parser, browser_worker,
dom_healer) y la memoria/IA son el aporte clave. La orquestación
(`scraper.py`, `run_with_kiro.py`) es ad-hoc y queda fuera. El telegram_sender
es referencia pero el nuevo proyecto usa Telegram solo para escuchar, no enviar.

## 2. Tabla de módulos

| Path | Líneas | Propósito | Calidad | Veredicto |
|---|---|---|---|---|
| `scraper.py` | ~250 | Entrypoint con CLI | Aceptable | DESCARTAR |
| `run_with_kiro.py` | ~270 | Orquestador con kiro-cli + análisis DOM | Aceptable | DESCARTAR |
| `mcp_server.py` | ~900 | Servidor MCP con 6 tools de exploración | Aceptable | ADAPTAR |
| `telegram_sender.py` | ~140 | Envía ofertas a Telegram con foto | Limpio | REFERENCIA (formato) |
| `send_pending.py` | ~50 | Backfill de ofertas pendientes | Limpio | DESCARTAR |
| `setup_kiro_login.py` | ~100 | Device-flow login para kiro-cli | Limpio | ADAPTAR |
| `test_scraper.py` | ~250 | Suite de tests funcionales | Aceptable | DESCARTAR (rehacer en pytest) |
| `test_telegram.py` | ~50 | Test manual de envío | Aceptable | DESCARTAR |
| `test_mcp.py` | varios | Test MCP | n/a | DESCARTAR |
| `src/browser_worker.py` | ~430 | Playwright Amazon con stealth + captcha + warmup | Limpio | ADAPTAR |
| `src/price_parser.py` | ~350 | Extracción de precios Amazon (search + product) | Limpio | REUTILIZAR_LITERAL |
| `src/dom_healer.py` | ~180 | Detect degradación + kiro-cli para healing | Aceptable | ADAPTAR |
| `src/memory_store.py` | ~210 | JSON store de aprendizaje | Aceptable | REESCRIBIR (a SQLite) |
| `src/ai_evaluator.py` | ~300 | Eval por kiro-cli/Anthropic/reglas | Aceptable | ADAPTAR |
| `config/settings.json` | 27 | Settings | Limpio | REFERENCIA |
| `config/selectors.json` | 50 | Selectores Amazon (auto-healed) | Limpio | REUTILIZAR_LITERAL |
| `config/seeds.json` | 40 | Seeds Amazon | Limpio | REUTILIZAR_LITERAL |
| `data/heal_samples/*.html` (97) | n/a | Snapshots de DOM Amazon | n/a | REUTILIZAR (fixtures) |
| `MEMORY.md` | ~250 | Memoria operacional con secretos VPS | n/a | REUTILIZAR (sin secretos) |
| `run_bot.ps1` | n/a | Wrapper PowerShell | n/a | DESCARTAR |
| `prompt.txt` | n/a | Prompt sample | n/a | DESCARTAR |

## 3. Áreas analizadas

### 3.1 scraper.py / run_with_kiro.py

- `scraper.py` setup logging coloreado, carga seeds priorizados, ejecuta
  `BrowserWorker.run_session`. Modos: `--continuous`, `--report`, `--max-offers`,
  `--no-ai`, `--no-headless`.
- `run_with_kiro.py` actúa como wrapper con tools: `--analyze-dom URL`,
  `--get-strategy` (consulta IA por estrategia, agrega URLs sugeridas
  a `seeds.json`).
- Ambos están atados al modelo single-marketplace y al MemoryStore JSON
  → se descartan, pero los modos `report` y `analyze-dom` son patrones
  útiles para CLI tools del nuevo proyecto.

### 3.2 src/browser_worker.py

Características valiosas:
- **STEALTH_SCRIPT** completo (oculta `webdriver`, `plugins`, `languages`,
  `chrome.runtime`, `permissions`, `hardwareConcurrency`, `deviceMemory`,
  `platform`, `vendor`).
- **5 user agents Chrome** y **5 viewports** rotables.
- **`_warmup`**: visita homepage primero para cookies legítimas.
- **`_handle_captcha`**: detecta `validateCaptcha`, `amzn-captcha`,
  texto "Enter the characters". Backoff exponencial 30s → 300s.
- **`_human_delay`**, **`_human_scroll`**, **`_move_mouse_randomly`**.
- **`_navigate`** con retry sobre 503, manejo 200/301/302/404.
- **`HIGH_RISK_PATTERNS`**: 14 patrones URL que evita.
- Logic propia de `run_session`: maneja frontier, captcha streak,
  re-seed, save_state, save_offers, IA enrichment.

Vale para `marketplaces/amazon/browser.py` (adaptado al BrowserManager
genérico del nuevo proyecto).

### 3.3 src/price_parser.py

- `parse_price_text` detecta formato MX (`1,299.00`), europeo (`1.299,00`),
  decimales y miles.
- `extract_discount_from_text` regex `(\d{1,3})\s*%`.
- `calculate_discount(current, original)`.
- `extract_product_data_from_page` con 5+ selectores fallback para
  `price_current`, `price_original`, `discount_badge`, `coupon`,
  `availability`, `rating`, `reviews`, `category`.
- `extract_products_from_search` extrae listas con detección de precio
  tachado vs normal (`a-text-price` clase del padre).

**Pieza clave para Amazon.** Reutilizar literal.

### 3.4 src/dom_healer.py

Flujo:
1. `DegradationMonitor` deque(window=20) trackea ratio de éxito.
2. Si failure_rate ≥ 0.4 → `is_degraded()` true.
3. `DomHealer.heal(page, context)` con cooldown 5min:
   - captura HTML relevante (search → 3 containers; product → `#corePrice_desktop`/`#price`).
   - guarda en `data/heal_samples/sample_<ts>.html`.
   - llama `kiro-cli chat --no-interactive --model claude-sonnet-4-5`
     con prompt corto (selectores fallando + HTML snippet ≤4000 chars).
   - parsea JSON respuesta, mergea en `selectors.json`, incrementa
     `_heal_count`, registra en MemoryStore.

Cosas a mejorar al adaptar:
- Soportar Mercado Libre además de Amazon (selectores por marketplace).
- Persistir snapshots en SQLite (`dom_snapshots`, `selector_versions`).
- Volver el llamado a IA opcional (heurísticas primero, IA solo si fallan).
- Disparar tests automáticos del fixture nuevo antes de aplicar el patch.

### 3.5 src/memory_store.py

JSON con esquema v2:
- `stats`: pages_visited, products_evaluated, offers_found, ai_calls, dom_heals, sessions.
- `category_performance`: por categoría {visits, offers_found, total_discount, avg, score}.
- `selector_failures`/`successes`: contadores por selector.
- `seen_urls` (5000 cap), `seen_asins` (10000 cap).
- `ai_evaluations`: lista 500 últimas {ts, asin, title, discount, verdict, reasoning}.
- `dom_heal_history`: lista de healings.
- `learned_patterns`: high_discount_keywords, low_quality_patterns.
- `session_history`: últimas 50 sesiones.

→ Migra a tablas SQLite: `agent_runs`, `selector_versions`, `dom_snapshots`,
`memory_summaries`. Caps numéricos pasan a queries con `ORDER BY ts DESC LIMIT N`.

### 3.6 src/ai_evaluator.py

- Detecta backend disponible al import: `kiro-cli` autenticado >
  `ANTHROPIC_API_KEY` > reglas.
- `_rules_evaluate`: descuento + rating + reviews + Prime + deal,
  penaliza títulos "genérico/sin marca/noname/compatible".
- `evaluate_offer`: prompt JSON, throttle 1s, parsea con regex.
- `evaluate_batch`: array JSON para múltiples ofertas.
- `analyze_html_for_prices`: fallback IA cuando selectores fallan.
- `generate_search_strategy`: input = memory summary, output = priority_categories,
  new_keywords.

Adaptar como `intelligence/llm_evaluator.py` separado de `intelligence/price_error_scorer.py`
(la IA es opcional; el scorer es determinista).

### 3.7 mcp_server.py

Tools expuestas:
- `navigate_and_extract(url)` — navegación + extracción + clasificación
  (búsqueda/producto/deals/categoría).
- `generate_exploration_urls(strategy, focus, count)` — genera URLs de
  Amazon MX (deals, categorías por node ID, keywords).
- `save_offer(asin, title, ...)` — guarda + envía a Telegram.
- `record_url_performance(url, offers_found, products_seen)` — stats.
- `get_intelligence_report` — reporte agregado.
- `extract_links_from_current_page(filter)` — links de navegación.

→ Adaptar a `agents/mcp_amazon_hunter.py` exponiendo tools genéricas
multi-marketplace cuando se necesite control humano vía Kiro CLI.

### 3.8 telegram_sender.py

Formato actual (con foto):

```
*Título del producto*

🔥 *X% de descuento*
❌ Antes: $ORIGINAL
✅ *AHORA: $ACTUAL MXN*
⭐ rating/5 (N reseñas)
📂 categoría

👉 *Ver oferta:*
https://amzn.to/...
```

→ El nuevo proyecto **no envía a Telegram**, sólo escucha canales
(Telethon). El formato sirve como **referencia** del formato de oferta
normal a WhatsApp (corregido al formato JBL Tune 510BT del usuario).

### 3.9 setup_kiro_login.py

- Usa `kiro-cli login --license free --use-device-flow`.
- Lee stdout línea por línea hasta detectar `Code:`.
- Abre browser a `https://view.awsapps.com/start/#/`.
- Loop con timeout 180s.
- → Mover a `scripts/kiro_login.py` adaptado y manual.

### 3.10 Snapshots `data/heal_samples/`

97 archivos HTML reales de Amazon que el DomHealer usó como muestras.
Vale la pena promover algunos a `tests/fixtures/snapshots/amazon/` para
tests deterministas.

## 4. Reportaje sobre DOM Healer

**Robustez:**
- Cooldown 5min — bien para evitar loops.
- Failure rate window=20, threshold=40% — razonable.
- Captura HTML por contexto (search/product) — bien.
- Llama a kiro-cli con prompt corto — funciona.
- Persiste sample HTML — bien para debugging.

**Limitaciones:**
- No corre tests del fixture antes de aplicar el patch.
- Mergea sin diff revisable.
- No revierte si después del patch sigue fallando.
- Acoplado a `selectors.json` (no soporta selectors versionados).
- Solo Amazon.

**Mejoras al adaptar:**
- Tabla `selector_versions(version, marketplace, context, key, selector,
  applied_at, applied_by, reverted_at)`.
- Tabla `dom_snapshots(id, url, marketplace, captured_at, content,
  reason)`.
- Antes de aplicar patch: ejecutar pytest sobre `tests/fixtures/snapshots/<context>/<sample>.html`.
- Si fallan, no aplica.

## 5. Piezas a reutilizar literal

1. `src/price_parser.py` (renombrar a `marketplaces/amazon/price_parser.py`).
2. `config/selectors.json` (mover a `config/selectors/amazon.json`).
3. `config/seeds.json` (mover a `config/seeds/amazon.json`).
4. STEALTH_SCRIPT, USER_AGENTS, VIEWPORTS de `browser_worker.py`.
5. Patrones de captcha detection.
6. `data/heal_samples/sample_*.html` → seleccionar 5-10 más representativos
   como fixtures.
7. Formato de mensaje WhatsApp del `telegram_sender.py` (estructura).

## 6. Piezas a adaptar

1. `BrowserWorker` → `marketplaces/amazon/hunter.py` con clase `AmazonHunter`.
2. `DomHealer` + `DegradationMonitor` → `self_healing/dom_healer.py`
   multi-marketplace + tests automáticos antes de patch.
3. `AIEvaluator` → `intelligence/llm_evaluator.py` (modos kiro/anthropic/none)
   y `intelligence/price_error_scorer.py` separado.
4. `mcp_server.py` → `agents/mcp_server.py` exponiendo tools genéricas.

## 7. Piezas a reescribir o descartar

1. **Reescribir:** `MemoryStore` → SQLite (tablas múltiples).
2. **Descartar:** `scraper.py`, `run_with_kiro.py`, `send_pending.py`,
   `test_scraper.py` (rehacer en pytest), `test_telegram.py`, `test_mcp.py`,
   `run_bot.ps1`, `prompt.txt`.

## 8. Riesgos

- **Hardcoded paths a kiro-cli** — `c:\Users\Programador Mx\AppData\Local\kiro-cli\kiro-cli.exe`.
  Mover a env var `KIRO_CLI_PATH` o `shutil.which("kiro-cli")`.
- **Bot Token Telegram + chat_id hardcoded** en `telegram_sender.py`.
  Mover a `.env`.
- **`MEMORY.md` con credenciales VPS** (mismo problema que el otro proyecto).
- **`mcp_server.py` con paths absolutos** del `data/`.

## 9. Modelo de datos actual

- `Offer` (dict en JSON):
  ```
  asin, title, url, price_current, price_original, discount_percent,
  discount_source, has_deal, has_coupon, rating, reviews, category,
  availability, raw_html_snippet, image_url, source_url, found_at,
  ai_evaluation: {verdict, score, genuine_discount, reasoning, emoji,
  short_description, tags}
  ```
- `Memory`:
  ```
  stats, category_performance, selector_failures, seen_urls, seen_asins,
  ai_evaluations, dom_heal_history, learned_patterns, session_history,
  seed_performance
  ```

Ambos se fusionan en el schema SQLite del nuevo proyecto (ver
`docs/architecture.md`).
