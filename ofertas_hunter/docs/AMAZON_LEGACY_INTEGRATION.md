# AmazonScrapperIA legacy → ofertas_hunter (Fase 1)

Integración del scraper anti-captcha del proyecto legacy
`AmazonScrapperIA` como hunter Amazon alternativo del bot
`ofertas_hunter`. La integración mantiene **el scoring del bot nuevo**
(`PriceErrorScorer`, `AccessoryDetector`, salvaguardas anti-falsos
positivos) y solo aprovecha el legacy para el scraping/extracción del
DOM Amazon.

> Status: Fase 1 (en repo local). El servidor VPS todavía corre el
> hunter Amazon original. Activación opt-in por flag.

---

## TL;DR

```bash
# Activar el hunter legacy:
echo "AMAZON_HUNTER_LEGACY=true" >> .env

# Volver al hunter nuevo (default):
sed -i '/AMAZON_HUNTER_LEGACY=/d' .env
# o setearlo explícitamente a false:
echo "AMAZON_HUNTER_LEGACY=false" >> .env
```

Solo un hunter Amazon corre a la vez. ML, Telegram y dispatcher no
cambian.

---

## Por qué se hizo

El hunter Amazon nuevo (`AmazonHunterAgent`) usa Playwright con
`launch_persistent_context` apuntando a `secrets/browser_profiles/amazon`.
En producción VPS, después de unas horas, ese perfil persistente
empezó a recibir captchas en cadena (Amazon detecta fingerprinting
estable y exige captcha). Ver eventos
`amazon_captcha_confirmed` + `mcp_marketplace_paused` en
`runtime_events`.

El scraper legacy `AmazonScrapperIA` históricamente nunca dio
captchas en producción local. Su receta:

- `chromium.launch` + `new_context` (browser **efímero**, sin profile
  persistente).
- `USER_AGENTS` rotables (5 versiones Chrome 122-124).
- `VIEWPORTS` aleatorios (5 tamaños 1280-1920).
- Headers HTTP completos (`Sec-Ch-Ua`, `Sec-Fetch-*`,
  `Accept-Language`, `Cache-Control`).
- `STEALTH_SCRIPT` inyectado en cada nueva página (oculta
  `navigator.webdriver`, `plugins`, `chrome.runtime`,
  `permissions.query`, `hardwareConcurrency`, `deviceMemory`,
  `platform`, `vendor`).
- Delays con distribución gaussiana truncada (no uniforme).
- Scroll humano gradual (con vueltas hacia arriba aleatorias).
- Movimientos de mouse aleatorios (2-4 puntos).
- Reintento suave en 503 (espera 15-30s, luego segundo intento).

Esa receta sigue siendo viable. La adoptamos como hunter alternativo
opt-in.

---

## Qué se copió (y dónde quedó)

```
ofertas_hunter/src/ofertas_hunter/agents/legacy_amazon/
├── __init__.py             # módulo namespaced
├── selectors.json          # copia exacta de AmazonScrapperIA/config/
├── price_parser.py         # copia + extensión (image_url, brand)
├── worker.py               # NUEVO: receta anti-captcha minimal
└── adapter.py              # NUEVO: legacy dict → Product+Offer+Outbox
```

```
ofertas_hunter/src/ofertas_hunter/agents/
└── legacy_amazon_hunter_agent.py   # NUEVO: hunter público
```

```
ofertas_hunter/src/ofertas_hunter/
├── config.py        # +1 flag: amazon_hunter_legacy: bool
└── orchestrator.py  # +switch en make_amazon_hunter_factory
```

---

## Qué NO se copió

Del paquete legacy `AmazonScrapperIA/src/`:

- **`browser_worker.py`**: tenía un frontier interno propio,
  `MemoryStore` persistente, `DomHealer` con calls a kiro-cli y
  manejo de sesiones largas. Reemplazado por `worker.py` minimal que
  solo expone `fetch_product(url)` y comparte el frontier de
  `ofertas_hunter`.
- **`memory_store.py`**: el bot nuevo ya tiene SQLite con tablas
  `products`, `offers`, `price_observations`, `discarded_candidates`,
  `runtime_events`. No necesitamos un JSON paralelo.
- **`dom_healer.py`**: dependía de `kiro-cli` para heal automático
  (subprocess.run a Claude Sonnet). Innecesario para Fase 1: si los
  selectores se rompen, lo arreglamos manualmente.
- **`ai_evaluator.py`**: tenía heurísticas paralelas
  (`EXCELENTE/BUENA/REGULAR/DESCARTAR`) que NO usamos. La
  clasificación final viene de `PriceErrorScorer`.
- **`scraper.py`** (top-level): tenía `run_adaptive_session`,
  `_seed_frontier`, etc. No aplicamos nada de eso.

---

## Por qué el legacy se usa solo para scraping

**Criterio A** de la spec del usuario:

> No quiero volver al scoring legacy crudo como fuente de verdad.

El legacy usaba:

```python
# AmazonScrapperIA/src/ai_evaluator.py
def _rules_evaluate(product: dict) -> dict:
    score = 50
    if discount >= 70: score += 30
    elif discount >= 60: score += 20
    elif discount >= 50: score += 10
    if rating >= 4.0: score += 10
    if reviews >= 100: score += 10
    if product.get("has_prime"): score += 5
    if product.get("has_deal"): score += 5
    suspicious = ["genérico", "sin marca", "noname", "compatible"]
    if any(s in title.lower() for s in suspicious):
        score -= 20
```

Ese scoring **NO** tiene:

- Detector de accesorios genéricos (cargadores, audífonos, fundas).
- Whitelist premium audio (Sony WF-1000XM, AirPods Pro, QC Ultra).
- Cap a `suspicious_deal` (55) para accesorios.
- Distinción smartphone-real vs accesorio-de-marca-celular.
- Reglas de penalización por título "compatible con iPhone".
- Penalización de variant_mismatch.

Esas reglas viven en `ofertas_hunter/intelligence/` y son las que
evitaron los falsos positivos:

- `accessory_detector.py` — Redmi Buds, JBL Flip, Sony WH-CH520, etc.
- `price_error_scorer.py` — `_fingerprint_smartphone`, etc.

Por eso el adapter convierte el `dict` del legacy a un
`PriceErrorSignal` y deja que `PriceErrorScorer` decida la
clasificación. Tests:

- `test_legacy_amazon_adapter_uses_new_price_error_scorer`
- `test_legacy_amazon_adapter_does_not_use_legacy_verdict_as_final_truth`
- `test_legacy_amazon_adapter_does_not_promote_audio_to_price_error`

---

## Cómo activarlo

### Local

```powershell
# 1) En el .env del repo:
Add-Content -Path .env -Value "AMAZON_HUNTER_LEGACY=true"

# 2) Correr el orquestador como siempre:
.\.venv\Scripts\python.exe scripts\orquestador_ia.py
# o
.\.venv\Scripts\python.exe -m ofertas_hunter run
```

En los logs deberías ver:

```
[Amazon] Loop arrancado
amazon_legacy hunt: procesados=5 encolados=2 descartados=3 captchas_high=0
```

(Note el prefijo `amazon_legacy` vs el `amazon` del hunter nuevo.)

### VPS (cuando esté listo)

```bash
ssh ... agaetranahoy@34.59.242.95
cd /opt/deal-agent/ofertas_hunter
echo "AMAZON_HUNTER_LEGACY=true" | sudo tee -a .env

# Reiniciar systemd o el orquestador IA según modo activo:
sudo systemctl restart ofertas-hunter   # daemon Python
# o detener el kiro-cli orquestador y relanzar
```

---

## Cómo volver al hunter nuevo

```powershell
# Quitar la línea del .env (o setear false explícito):
(Get-Content .env) -notmatch '^AMAZON_HUNTER_LEGACY' | Set-Content .env

# Reiniciar el orquestador.
```

---

## Diferencias visibles en runtime

| Componente              | Hunter nuevo                     | Hunter legacy                                  |
|-------------------------|----------------------------------|------------------------------------------------|
| Browser                 | `PlaywrightBrowserWorker` persist| `LegacyAmazonWorker` efímero                   |
| Profile cookies         | `secrets/browser_profiles/amazon`| Sin profile (cookies por sesión)               |
| User-Agent              | Fijo (Chrome 124)                | Random rotativo cada sesión                    |
| Viewport                | Fijo (1920x1080)                 | Random (5 tamaños)                             |
| Stealth init script     | Sí                               | Sí (legacy)                                    |
| Delay entre requests    | 5-12s                            | 8-15s + distribución gaussiana                 |
| Captcha detector        | `AmazonCaptchaDetector` central  | `_is_real_captcha` legacy (URL + 2+ señales)   |
| Decisión de pausa       | `should_pause_marketplace`       | Solo confidence='high' pausa (criterio C)      |
| DOM incompleto          | Tratado como blocked             | Distinguido de captcha (criterio C)            |
| `image_url` requerida   | Sí                               | Sí (gate del adapter)                          |
| `current_price` requerida| Sí                              | Sí (gate del adapter)                          |
| Scoring final           | `PriceErrorScorer`               | `PriceErrorScorer` (mismo, criterio A)         |
| Frontier                | Compartido `kind=product`        | Compartido `kind=product` (criterio 5)         |
| Discovery               | DiscoveryAgent en mismo loop     | DiscoveryAgent (sin cambios, criterio 5)       |

---

## Criterios de aceptación cubiertos

- ✅ **A**: scraping legacy + scoring nuevo (no `EXCELENTE/BUENA/...`).
- ✅ **B**: solo un hunter Amazon corre. El switch está en
  `orchestrator.make_amazon_hunter_factory`. Test
  `test_only_one_amazon_hunter_runs_at_a_time`.
- ✅ **C**: detector legacy distingue captcha real (URL +
  `form_action_validate_captcha`/`captchacharacters`) de DOM
  incompleto. Test `test_legacy_amazon_does_not_pause_on_missing_price_as_captcha`.
- ✅ **D**: el adapter rechaza si falta `image_url`, `current_price`,
  `url`, `title`. Tests
  `test_legacy_amazon_adapter_requires_image|current_price|url`.
- ✅ **E**: razones registradas en `discarded_candidates` y
  `runtime_events`. Test
  `test_legacy_amazon_adapter_records_discard_reason_in_runtime_events`.
- ✅ **F**: 11 tests obligatorios + 11 adicionales = 22 tests legacy.
- ✅ **G**: este documento.
- ✅ **H**: pytest completo (582 passing), Mercado Libre/Telegram/
  dispatcher intactos (sus tests pasan sin tocarlos).

---

## Cómo correr una prueba real de 30 minutos

```powershell
# 1) Asegurar flag en .env:
Add-Content -Path .env -Value "AMAZON_HUNTER_LEGACY=true"

# 2) Verificar tests primero:
.\.venv\Scripts\python.exe -m pytest tests/unit/agents/legacy_amazon/ -v

# 3) Lanzar el orquestador:
.\.venv\Scripts\python.exe scripts\orquestador_ia.py
```

Mientras corre, en otra terminal monitorear:

```powershell
# Ver eventos captcha legacy:
.\.venv\Scripts\python.exe -c "
import sqlite3
con = sqlite3.connect('data/ofertas_hunter.db')
con.row_factory = sqlite3.Row
for r in con.execute('''
    SELECT created_at, severity, kind, substr(payload_json, 1, 200) AS p
    FROM runtime_events
    WHERE kind LIKE 'amazon_legacy_%'
    ORDER BY id DESC LIMIT 30
'''):
    print(r['created_at'], r['severity'], r['kind'])
    print('  ', r['p'])
"

# Ver outbox encolado por el legacy:
.\.venv\Scripts\python.exe -c "
import sqlite3, json
con = sqlite3.connect('data/ofertas_hunter.db')
con.row_factory = sqlite3.Row
for r in con.execute('''
    SELECT id, type, state, message_payload_json
    FROM outbox
    ORDER BY id DESC LIMIT 20
'''):
    p = json.loads(r['message_payload_json'])
    extractor = p.get('extractor', '?')
    print(f'  #{r[\"id\"]:5} {r[\"type\"]:13} {r[\"state\"]:9} extractor={extractor:20} {p.get(\"title\",\"\")[:50]}')
"
```

Criterio de éxito (30 min):

- ≥ 5 ofertas encoladas con `extractor=legacy_amazon`.
- 0 eventos `amazon_legacy_captcha_confirmed`.
- 0 falsos positivos `price_error_confirmed` en accesorios.
- 0 ítems publicados sin `image_url` o `current_price`.

Si aparece algún `amazon_legacy_captcha_confirmed` con `confidence=high`,
el legacy también está siendo bloqueado y la receta anti-captcha ya no
basta: hay que rotar IP del VPS o pasar el captcha manualmente.

Si aparecen `amazon_legacy_captcha_suspect` (medium), es esperado:
indica páginas con DOM raro pero no se considera captcha real.

---

## Próximos pasos (futuras fases)

- **Fase 2**: si la prueba de 30 min es estable, subir al VPS
  (`AMAZON_HUNTER_LEGACY=true` en `/opt/deal-agent/ofertas_hunter/.env`)
  y reiniciar el servicio.
- **Fase 3**: integrar `DegradationMonitor` legacy en el adapter para
  emitir alertas cuando los selectores fallan ≥ 40% en una ventana
  móvil de 20 fetches.
- **Fase 4** (opcional): permitir que el legacy haga discovery propio
  además de procesar el frontier (criterio 5 spec actual lo prohíbe
  para Fase 1).

