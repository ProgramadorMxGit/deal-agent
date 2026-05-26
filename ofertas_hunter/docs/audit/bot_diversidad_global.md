# Auditoría — bot_diversidad_global

> Fecha: 2026-05-25
> Alcance: `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\bot_diversidad_global`
> Propósito: identificar piezas reutilizables para `ofertas_hunter`.

## 1. Resumen ejecutivo

`bot_diversidad_global` es un crawler asíncrono de Mercado Libre MX en Python 3.11+
con `asyncio` + Playwright. Su propósito es encontrar productos con descuento real
≥ 50% y un botón "Compartir" activo (afiliado), extraer el link `meli.la`, evaluar
con `kiro-cli` (Claude Sonnet 4.5) y guardar en `data/offers.json`.

**Veredicto global:** la base arquitectónica modular (explorers, extraction, storage,
session_loader, models, tests) es sólida y vale la pena tomarla como referencia.
La orquestación (single-worker disfrazada de pool), la persistencia JSON, el
cookie_renewal_notifier (acoplado al otro VPS) y el `_notify_kiro_auth_needed`
(threading frágil) deben rediseñarse o descartarse.

## 2. Tabla de módulos

| Path | Líneas | Propósito | Calidad | Veredicto |
|---|---|---|---|---|
| `src/main.py` | ~110 | CLI entrypoint, modos test/continuous/evaluate-url | Limpio | ADAPTAR |
| `src/orchestrator.py` | ~280 | Loop principal, frontier, cookie expiry detection | Mejorable (single-worker) | REESCRIBIR |
| `src/browser_worker.py` | ~190 | Wrapper Playwright para ML (cookies, `extract_affiliate_link`) | Aceptable | ADAPTAR |
| `src/session_loader.py` | ~140 | Carga + sanitiza cookies (Cookie-Editor format) | Limpio | REUTILIZAR_LITERAL |
| `src/models.py` | ~50 | Dataclasses `Offer`, `FrontierItem`, `CrawlStats` | Limpio | ADAPTAR (a SQLite) |
| `src/utils.py` | ~150 | Canonicalize URL, blocklist, regex de productos ML | Limpio | REUTILIZAR_LITERAL |
| `src/config.py` | ~30 | Carga settings/seeds JSON | Limpio | REUTILIZAR_LITERAL |
| `src/cookie_renewal_notifier.py` | ~200 | Notifica cookies vencidas vía Evolution API + escribe state cross-repo | Mejorable (acoplamiento) | REESCRIBIR |
| `src/explorers/category_explorer.py` | ~50 | Clasifica URLs y descubre categorías | Limpio | REUTILIZAR_LITERAL |
| `src/explorers/listing_explorer.py` | ~30 | Extrae productos y links de listados | Limpio | REUTILIZAR_LITERAL |
| `src/explorers/product_explorer.py` | ~50 | Extrae precio + share button + affiliate link | Limpio | REUTILIZAR_LITERAL |
| `src/extraction/price_parser.py` | ~110 | Extrae precio actual/anterior del DOM real ML | Limpio | REUTILIZAR_LITERAL |
| `src/extraction/product_parser.py` | ~80 | Detecta share button, título, calcula descuento | Limpio | REUTILIZAR_LITERAL |
| `src/evaluation/offer_evaluator.py` | ~220 | Hard rule 50%, llama kiro-cli, fallback `pendiente_llm` | Mejorable (threading auth) | ADAPTAR (sin device flow auto) |
| `src/storage/json_store.py` | ~110 | Save offers con asyncio lock + dedup | Aceptable (no escala) | REESCRIBIR (a SQLite) |
| `src/storage/state_store.py` | ~100 | Frontier + visited + stats en JSON | Aceptable | REESCRIBIR (a SQLite) |
| `tests/conftest.py` | 12 | Marker `smoke` | Limpio | REUTILIZAR_LITERAL |
| `tests/unit/test_*.py` (9 archivos) | varios | 48+ tests offline deterministas | Limpio | REUTILIZAR_LITERAL |
| `tests/integration/test_smoke_real_page.py` | varios | Smoke con snapshot HTML | Aceptable | ADAPTAR |
| `scripts/check_cookies.py` | ~40 | Verifica que cookies se carguen | Limpio | ADAPTAR |
| `scripts/capture_snapshot.py` | ~50 | Captura HTML real para fixtures | Limpio | REUTILIZAR_LITERAL |
| `scripts/test_affiliate_link.py` | ~50 | Test aislado del modal Compartir | Limpio | REUTILIZAR_LITERAL |
| `scripts/enrich_affiliate_links.py` | ~80 | Backfill de affiliate links | Aceptable | DESCARTAR (one-shot) |
| `scripts/debug_modal.py`, `debug_price.py` | varios | Debug ad-hoc | Aceptable | DESCARTAR |
| `scripts/*.sh` (≈25) | varios | Scripts SSH al VPS, deploy, monitor | Mezclados | REVISAR (varios cross-repo) |
| `deploy/systemd/bot-diversidad-global.service` | 17 | Service file con placeholders | Limpio | ADAPTAR |
| `config/settings.example.json` | 14 | Settings de referencia | Limpio | ADAPTAR |
| `config/seeds.example.json` | 10 | Seeds ML | Limpio | ADAPTAR |
| `connect-vps.ps1` | n/a | Conexión SSH al VPS con secretos hardcoded | Mejorable | DESCARTAR (mover a env) |

## 3. Áreas analizadas

### 3.1 Orchestrator y main

- Loop asíncrono con un solo browser worker activo (`self._workers[0]`),
  aunque el código instancia `n_workers`.
- Lee frontier de `state.json`, hace pop por score descendente.
- Maneja Ctrl+C: hace flush de state al terminar.
- Detecta redirección a `/account-verification` y dispara
  `notify_cookie_expiry()` (umbral 1).
- **Problema:** `n_workers > 1` no funciona realmente porque sólo se usa
  `self._workers[0]`, y `state_store` tiene un `asyncio.Lock` global
  (cuello de botella si hubiera concurrencia real).

### 3.2 BrowserWorker

- Lanza Chromium (no persistent context — explica nota de MEMORY.md sobre
  no usar `launch_persistent_context`).
- Inyecta cookies en context (es-MX, America/Mexico_City).
- Métodos clave: `get_page_html`, `extract_links`, `extract_affiliate_link`,
  `detect_degradation`.
- `extract_affiliate_link`: navega al producto, hace clic en
  `[data-testid="generate_link_button"]`, espera modal "Generar link",
  hace polling sobre `[data-testid="text-field__label_link"]` y
  `[data-testid="text-field__label_id"]`. **Esta lógica es valiosa.**
- `is_cookie_expiry_redirect` detecta `gz/account-verification`,
  `jms/mlm/lgz/login`, etc.

### 3.3 Explorers

- `category_explorer.classify_url` — clasifica `category | listing | product | unknown`.
- `listing_explorer.explore_listing` — extrae productos + nuevos URLs frontier.
- `product_explorer.explore_product` — orquesta `get_page_html` + `extract_product_data`
  + click en Compartir si hay share button.

### 3.4 Extraction

- `price_parser.extract_prices` — combina `andes-money-amount__fraction`
  + `andes-money-amount__cents`, fallback a `aria-label`. Maneja
  estructura ML actual (`ui-pdp-price__second-line`,
  `ui-pdp-price__original-value`).
- `product_parser.has_share_button` — primario `[data-testid="generate_link_button"]`,
  secundario texto "Compartir" en `.toolbar__actions`, terciario
  cualquier botón "Compartir".
- `product_parser.extract_product_data` — aplica regla dura:
  si no hay share button, retorna `evaluation_label="descartada"`.

### 3.5 Evaluation

- `offer_evaluator.apply_hard_rule(50%)` — separación correcta de la regla dura.
- `call_kiro_cli` — subprocess con `kiro-cli chat --agent ml-offer-evaluator
  --no-interactive '<json>'`, timeout 30s, fallback a `pendiente_llm`.
- **Problema:** `_notify_kiro_auth_needed` usa `subprocess.Popen`
  + `threading.Thread` con queue para device-flow. Es frágil y debería
  delegarse a un canal de runtime events / WhatsApp alert sin reintentos.

### 3.6 Storage

**`json_store.JsonStore`:**
- `_offer_qualifies` chequea share + affiliate + precios + descuento ≥50%.
- Dedup por id; actualiza solo si new_discount > existing + 10pts.
- Read-modify-write completo del archivo bajo asyncio lock.

**`state_store.StateStore`:**
- Carga/guarda todo el estado (frontier, visited, stats, cooldowns,
  recent_errors) en un solo JSON.
- `pop_from_frontier` ordena el array entero por score cada llamada
  (O(n log n) por pop — no escala).
- `mark_visited`, `is_visited` usan `set()` desde `list` cada llamada.

### 3.7 Session loader

- Soporta dos formatos: Playwright/CDP (`expires`) y browser extension
  (`expirationDate`, `hostOnly`, `storeId`, `session`).
- Resuelve path por env var: `BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH` >
  `OFERTAS_MELI_BROWSER_COOKIES_PATH` > `secrets/mercadolibre_cookies.json`.
- Sanitiza `sameSite` (no_restriction → None), elimina campos no aceptados
  por Playwright.
- **Excelente módulo.** Pasa íntegro al nuevo proyecto.

### 3.8 Tests

- 9 archivos en `tests/unit/`:
  - `test_deduplication.py`
  - `test_discount_calculator.py`
  - `test_json_store.py`
  - `test_models.py`
  - `test_price_parser.py`
  - `test_product_parser.py`
  - `test_session_loader.py`
  - `test_share_button_detector.py`
  - `test_url_blocklist.py`
- 1 archivo en `tests/integration/` (smoke con snapshot real).
- Todos deterministas, sin red, mockean Playwright.
- **Reutilizar literal**, ajustando imports al nuevo paquete.

### 3.9 Scripts

Mezcla de:
- Útiles para desarrollo: `check_cookies.py`, `capture_snapshot.py`,
  `test_affiliate_link.py`, `debug_*.py`.
- Cross-repo (ofertas-detector-vps): `vps_*.sh`, `check_outbox*.sh`,
  `check_sent_whatsapp.sh`, etc. — deberían vivir en su proyecto destino,
  no aquí.
- One-shot: `enrich_affiliate_links.py`, `vps_clean_frontier*.sh`.

### 3.10 Deploy

- `deploy/systemd/bot-diversidad-global.service` con placeholders
  `__PROJECT_ROOT__`, `__SERVICE_USER__`, `__SHARED_COOKIES_PATH__`.
- Modelo simple, viable. **Adaptar** al nombre nuevo `ofertas-hunter`.

## 4. Piezas para reutilizar literal

1. `src/session_loader.py` (renombrar a `marketplaces/mercadolibre/session.py`).
2. `src/utils.py` (split en `marketplaces/mercadolibre/url.py` + `core/url_utils.py`).
3. `src/extraction/price_parser.py` (mover a `marketplaces/mercadolibre/price_parser.py`).
4. `src/extraction/product_parser.py` (mover a `marketplaces/mercadolibre/product_parser.py`).
5. `src/explorers/*.py` (mover a `marketplaces/mercadolibre/explorers/`).
6. `src/browser_worker.extract_affiliate_link` (preservar patrón modal).
7. `src/evaluation/offer_evaluator.apply_hard_rule` y `parse_kiro_response`.
8. `tests/unit/*.py` (los 9 archivos, ajustando imports).
9. `tests/conftest.py`.
10. `scripts/capture_snapshot.py`, `scripts/test_affiliate_link.py`.
11. `config/settings.example.json` y `seeds.example.json` (estructura).
12. Agentes Kiro `.kiro/agents/ml-offer-evaluator.json` (si aún existen).

## 5. Piezas para reescribir

1. **`orchestrator.py`** — diseñar para multi-marketplace + multi-agente real,
   con concurrencia controlada por límites por agente.
2. **`storage/json_store.py` y `state_store.py`** — reemplazar por SQLite
   con WAL. JSON queda solo para export/debug.
3. **`cookie_renewal_notifier.py`** — convertir en un evento de runtime
   (`runtime_events` table) que un agente `runtime_watchdog` consume
   y notifica vía Evolution API. Sin acoplamiento cross-repo.
4. **`evaluator._notify_kiro_auth_needed`** — quitar device flow auto.
   Limitar a registrar evento; el operador renueva manualmente o por hook.

## 6. Piezas para descartar

1. `scripts/vps_*.sh` (manejo de servidor — irá en docs/deploy).
2. `scripts/check_outbox*.sh`, `check_sent_whatsapp.sh`, `kiro_login_ofertasrdp.sh`,
   `monitor_loop.sh`, `reevaluate_pending_llm.sh`, `reset_outbox_discarded.sh`,
   `test_evolution_api.sh`, `test_kiro_*.sh`, `test_whatsapp_notify.sh`,
   `vps_audit.sh`, `vps_clean_frontier*.sh`, `vps_debug_share_button.sh`,
   `vps_deploy_full.sh`, `vps_discover.sh`, `vps_inspect_outbox.sh`,
   `vps_patch.sh`, `full_status.sh`, `check_*.sh` — son utilities
   ad-hoc que pertenecen a `_legacy/`.
3. `scripts/debug_modal_dump.html`, `scripts/debug_price_dump.html`
   — dumps versionados (3000+ líneas).
4. `scripts/enrich_affiliate_links.py` — script one-shot.
5. `scripts/check_id_field.py` — debug puntual.
6. `connect-vps.ps1` — credenciales hardcoded.

## 7. Riesgos y acoplamientos

- **Secretos VPS hardcoded** en `MEMORY.md` (IP, usuario, password root).
  Rotación urgente recomendada después de migrar.
- **Evolution API key hardcoded** en `cookie_renewal_notifier.py`.
- **Path absolutos `/opt/ofertas-detector-vps/...`** en `cookie_renewal_notifier.py`.
- **Acoplamiento bidireccional** vía filesystem con `ofertas-detector-vps`
  (lee/escribe `operational_state.json`, `browser_cookies.json`).
- **Worker pool fingido**: `_workers` es lista pero solo se usa `[0]`.
- **State JSON**: read-modify-write completo en cada operación.
- **Mojibake** en `commission_text` por encoding mixto.

## 8. Modelo de datos `Offer` actual

```python
@dataclass
class Offer:
    id: str                         # sha256(canonical_url)[:12]
    url: str
    canonical_url: str
    title: str = ""
    category_path: str = ""
    discovered_from: str = ""
    discovered_at: str              # ISO8601 UTC
    last_seen_at: str               # ISO8601 UTC
    has_share_button: bool = False
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    currency: str = "MXN"
    discount_percent: Optional[float] = None
    evaluation_label: str = "pendiente"
    evaluation_reason: str = ""
    meets_hard_rule_50_percent: bool = False
    extraction_confidence: str = "low"   # low | medium | high
    raw_signals: dict
    html_snapshot_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    session_used: bool = False
    affiliate_link: Optional[str] = None
    affiliate_product_id: Optional[str] = None
    commission_text: Optional[str] = None
```

Este modelo será el punto de partida del nuevo `Offer` en SQLite,
extendido con campos de Amazon (asin, image_url) y los específicos
de error de precio (price_error_score, classification, etc).
