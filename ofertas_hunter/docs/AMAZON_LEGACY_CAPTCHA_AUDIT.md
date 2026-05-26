# Auditoría — Amazon CAPTCHA: legacy vs `ofertas_hunter`

Documento de la corrección del 2026-05-26: alineamos la detección de
CAPTCHA con la estrategia que usaba el legacy `AmazonScrapperIA` y
quitamos los falsos positivos por substring.

---

## 1. Qué hacía `AmazonScrapperIA` (legacy)

`AmazonScrapperIA/src/browser_worker.py` implementaba una pipeline
anti-detección y un detector de captcha:

### Anti-detección

- Lanzaba Chromium con flags clave:
  - `--disable-blink-features=AutomationControlled`
  - `--no-sandbox`, `--disable-dev-shm-usage`, `--disable-extensions`,
    `--no-first-run`, `--disable-default-apps`, `--disable-infobars`,
    `--window-size=1920,1080`, `--start-maximized`
- Rotación aleatoria de **User-Agent** (4 UA Chrome reales) y
  **viewport** (5 tamaños) por sesión.
- `extra_http_headers` legítimos: `Accept-Language=es-MX`, `Sec-Ch-Ua=...`,
  `Sec-Fetch-Mode=navigate`, etc.
- `STEALTH_SCRIPT` inyectado vía `add_init_script`: oculta `webdriver`,
  patches a `navigator.plugins`, `navigator.languages`, `chrome.runtime`,
  `Permissions.query`, `hardwareConcurrency`, `deviceMemory`, `platform`,
  `vendor`.
- **Warmup**: antes del primer fetch real, visita
  `https://www.amazon.com.mx` para establecer cookies anónimas.
- Comportamiento humano: `_human_delay` (gauss `(min, max)`),
  `_human_scroll` (incrementos de 200–500px con pausas), movimientos
  random de mouse, jitter de 5–12s entre páginas.
- 503 → espera 15–30s y **reintenta una vez**; sólo después marca como
  fallo. Captcha → backoff exponencial (30s, 60s, 120s, 240s, hasta 300s)
  per-session.

### Detección de captcha

```python
def _is_captcha_page(self, url: str, content: str) -> bool:
    captcha_signals = [
        "validateCaptcha" in url,
        "robot" in url.lower(),
        "Enter the characters you see" in content,
        "Escribe los caracteres que ves" in content,
        "Type the characters you see" in content,
        'name="amzn-captcha' in content,
        'id="captchacharacters"' in content,
        "api.prod.captcha.us-east-1.amazonaws.com" in content,
    ]
    return any(captcha_signals)
```

Combinación de URL + estructura HTML + texto visible. Salvo el primer
chequeo (`validateCaptcha in url`), todos los demás son tokens
**estructurales** o **visibles** y por tanto difíciles de generar
falsos positivos.

### Comportamiento ante captcha

- Incrementaba `_captcha_count` y `_consecutive_captchas`.
- Esperaba `30 * 2^n` segundos (cap 300s).
- Si `_consecutive_captchas >= 3`, advertía sesión bloqueada.
- Si `_consecutive_captchas >= 5`, pausa larga (5 min) y reset del contador.
- **Re-encolaba la URL** al frente del frontier.

---

## 2. Qué hacía `ofertas_hunter` antes del fix

`browser/playwright_worker.py` tenía:

- Lanzamiento de Chromium con sólo 3 flags
  (`--disable-blink-features=AutomationControlled`, `--disable-dev-shm-usage`,
  `--no-sandbox`).
- **Sin warmup**.
- **Sin extra_http_headers**.
- **Sin retry on 503**.
- Detector ingenuo:

  ```python
  _CAPTCHA_TOKENS = (
      "validateCaptcha",
      "amzn-captcha",
      "Enter the characters you see below",
      "Lo sentimos, parece que está utilizando un programa automatizado",
  )

  if any(token in html for token in _CAPTCHA_TOKENS):
      blocked = True
  ```

- Sin distinguir **señal estructural** vs **substring en script**.
- `discarded_reason='captcha_detected'` se emitía siempre que `blocked=True`.
- `scripts/orquestador_ia.py loop_amazon` pausaba Amazon 10 minutos si
  `captchas == processed`, sin filtrar por confianza.

---

## 3. Diferencia exacta

| Eje                        | Legacy (`AmazonScrapperIA`)            | `ofertas_hunter` (antes)                  | `ofertas_hunter` (ahora) |
|----------------------------|----------------------------------------|--------------------------------------------|---------------------------|
| Detector                   | URL + estructural + visible            | substring en HTML                          | **`AmazonCaptchaDetector` estructural** |
| Confianza                  | binaria (capt vs no)                   | binaria                                    | high / medium / low       |
| Pausa por substring        | no                                     | sí (todo lo que matchee)                   | **no**                    |
| Pausa por low confidence   | no                                     | sí                                         | **no**                    |
| Warmup homepage            | sí                                     | no                                         | **opt-in via config**     |
| Extra HTTP headers         | sí                                     | no                                         | **sí**                    |
| Retry 503                  | sí (15–30s, una vez)                   | no                                         | **sí**                    |
| Backoff exponencial        | sí                                     | sí (sólo en config, sin uso real)          | sí (config)               |
| Reasons distintas          | n/a                                    | `captcha_detected`                         | `amazon_extraction_failed`, `amazon_possible_block_low_confidence`, `captcha_detected` |

---

## 4. Causa raíz del falso positivo

El detector previo trataba la presencia del substring `validateCaptcha`
en el HTML como evidencia suficiente. Aunque en la práctica
**Amazon no devuelve esa palabra dentro de páginas legítimas en
`amazon.com.mx`** (verificado contra 90 muestras del legacy y 43
snapshots reales del nuevo proyecto), la detección era frágil:

- Cualquier bundle JS futuro con la palabra disparaba el bloqueo.
- Cualquier comentario en HTML compartido entre páginas también.
- Cualquier endpoint declarado en JSON de telemetry.

Adicionalmente la pipeline tenía una vulnerabilidad de UX: aunque
**en este momento todas las detecciones eran reales**, una sola entrada
con `length(content)=0` (un timeout) se contabilizó como captcha y
disparó la pausa de marketplace.

---

## 5. Qué se portó del legacy

1. **Stealth args de Chromium**: lista completa de flags del legacy.
2. **`extra_http_headers`** con `Sec-Ch-Ua`, `Accept-Language`, etc.
3. **Warmup opt-in** (`BrowserConfig.warmup_amazon_homepage`).
4. **503 retry** (legacy: 15–30s, una vez) configurable vía
   `amazon_retry_on_503` y `amazon_retry_wait_range_seconds`.
5. **Detector estructural** con las mismas señales que el legacy
   (`form_action`, `captchacharacters`, `amzn-captcha`, texto humano)
   pero con clasificación high/medium/low.

---

## 6. Qué hace el nuevo detector

`browser/amazon_captcha_detector.py` implementa
`AmazonCaptchaDetector.assess(html, final_url, status)` y devuelve
`CaptchaAssessment`:

- **Señales fuertes** (estructurales): `form_action_validate_captcha`,
  `input_captchacharacters`, `input_amzn_captcha`, `img_src_captcha`,
  `url_validate_captcha`.
- **Señales visibles**: `title_robot_check`, `text_enter_characters_*`,
  `text_apology_es`, `text_continuar_comprando`, `text_continue_shopping`.
- **Señales débiles** (substrings): `validateCaptcha`, `amzn-captcha`,
  `captcha-instrumentation`. **Por sí solas NO declaran captcha real**.

### Reglas de confianza

- `high` = al menos 1 fuerte + (visible o body chico < 10KB con title
  Amazon.com.mx/Robot Check).
- `medium` = fuerte sin visible.
- `low` = sólo débiles (substring) sin estructura.
- `none` = sin nada.

### Pausa de marketplace

`should_pause_marketplace=True` sólo si:
- confidence `high`, **y**
- (URL `/errors/validateCaptcha` + form_action) **o** (estructural + visible).

Nada de pausas por low/medium.

---

## 7. Cambios en el agente (`AmazonHunterAgent`)

- Si `page.blocked` y `extras.captcha_assessment.confidence == "high"`:
  emite `runtime_event(kind="amazon_captcha_confirmed", severity="error")`.
  El loop del orchestrator sigue contando estos para decidir pausa.
- Si `extras.captcha_assessment.is_captcha` con confidence
  `medium`/`low`: emite `runtime_event(kind="amazon_suspected_false_captcha",
  severity="warning")` y descarta con razón
  `amazon_extraction_failed` o `amazon_possible_block_low_confidence`.
  **No** cuenta para pausa del marketplace.

---

## 8. Tests añadidos

`tests/unit/browser/test_amazon_captcha_detector.py`:

- `test_amazon_captcha_real_robot_check_title`
- `test_amazon_captcha_real_validate_captcha_form`
- `test_amazon_captcha_real_captchacharacters_input`
- `test_amazon_captcha_real_visible_robot_text`
- `test_amazon_captcha_real_url_validatecaptcha`
- `test_amazon_captcha_false_positive_script_contains_captcha`
- `test_amazon_captcha_false_positive_json_contains_captcha`
- `test_amazon_captcha_false_positive_telemetry_contains_captcha`
- `test_amazon_captcha_false_positive_product_page_with_captcha_word_in_script`
- `test_amazon_captcha_status_503_without_visible_robot_check_is_not_enough`
- `test_amazon_missing_price_is_not_captcha`
- `test_amazon_selector_miss_is_not_captcha`
- `test_amazon_timeout_is_not_captcha`
- `test_amazon_does_not_pause_on_low_confidence_captcha`
- `test_amazon_does_not_increment_captcha_counter_on_false_positive`
- `test_amazon_pauses_on_repeated_high_confidence_real_captcha`
- `test_amazon_pauses_on_validatecaptcha_url_and_form`
- `test_amazon_legacy_real_captcha_samples_classified_high` (90/90 muestras)
- `test_amazon_legacy_non_captcha_samples_not_marked` (7/7 muestras)
- `test_amazon_legacy_like_product_html_not_marked_captcha`
- `test_amazon_legacy_debug_product_not_marked_captcha`
- `test_amazon_legacy_debug_search_not_marked_captcha`

`tests/unit/agents/test_amazon_hunter_agent.py` (nuevos):

- `test_amazon_hunter_emits_confirmed_event_on_high_confidence_captcha`
- `test_amazon_hunter_does_not_pause_on_low_confidence_captcha`
- `test_amazon_hunter_emits_suspected_false_captcha_when_blocked_with_low_confidence`

Todos pasan: 538/538 (was 513).

---

## 9. CLI nuevos

```
python -m ofertas_hunter audit-amazon-captcha [--recent] [--fix] [--days N]
python -m ofertas_hunter amazon-captcha-check <url-or-snapshot-path>
```

`audit-amazon-captcha` recorre `dom_snapshots` Amazon recientes,
re-clasifica con el detector nuevo y emite
`runtime_event(kind="amazon_captcha_audit_kept" | "amazon_captcha_false_positive_reclassified")`.
Con `--fix` actualiza los `discarded_candidates` afectados.

`amazon-captcha-check` permite verificar una URL o un archivo HTML
sin modificar nada. Útil para reproducir reportes del usuario.

---

## 10. Verificación con datos reales

```
python -m ofertas_hunter audit-amazon-captcha --recent --fix --days 7
```

Resultado en la DB local actual:

```
scanned_snapshots:           43
real_captcha (high):         42
reclassified_false_positives: 1
discarded_reclassified:      1
runtime_events_emitted:      43
```

El único reclassified es el snapshot 231 (body vacío por timeout,
clasificado por error como captcha_detected). Los 42 restantes son
captchas reales de Amazon — Amazon **sí** está sirviendo CAPTCHAs al
nuevo proyecto, pero el detector ya no los confunde con páginas
normales y la pausa sólo dispara en confianza alta.

Para reducir la frecuencia con que Amazon sirve captchas, queda
disponible la opción `BrowserConfig(warmup_amazon_homepage=True)` y la
config heredada del legacy (delays más largos, etc.). El operador
puede activarlo desde `.env` / config sin tocar código.


---

## 11. Opción 3 del lanzador (orquestador_ia.py) — Fix Iteración 2

### Síntoma

Después de la iteración 1, el operador siguió viendo:

```
Captcha confirmado en https://www.amazon.com.mx/dp/B00DGQMJE0 (signals=['form_action_validate_captcha'])
[Amazon] ciclo #1 procesados=5 encoladas=0
[WARN] Amazon: 5 CAPTCHAs — pausando 10 min
```

### Causas raíz (dos, encadenadas)

1. **MCP browser "desnudo"**. La opción 3 ejecuta
   `scripts/orquestador_ia.py` → `MCP server` → `ServerContext.get_browser()`
   → `AmazonHunterAgent`. Ese `get_browser()` construía
   `BrowserConfig(headless=…)` SIN `user_data_dir`, sin warmup, sin
   headers stealth. La sesión persistente que el operador creó con
   `python -m ofertas_hunter login --marketplace amazon` NO se usaba
   en la opción 3. Resultado: Amazon servía captchas reales con alta
   frecuencia.

2. **`loop_amazon` contaba string equality**. Sumaba 1 por cada outcome
   con `discarded_reason == "captcha_detected"` y pausaba 10min sin
   leer `confidence` ni `should_pause_marketplace`. Cualquier
   estructura captcha (incluso medium sin visible) activaba la pausa.

3. **Cosmético: log incompleto**. El warning del worker imprimía sólo
   `strong_signals`, no incluía `visible_signals` ni `confidence`. Daba
   la impresión de que se estaba marcando captcha por `form_action`
   solo, cuando en realidad el detector ya estaba viendo
   `text_continuar_comprando` también.

### Cambios

#### `mcp/context.py`

- Tres browsers separados:
  - `_browser` (legacy, sin user_data_dir, fallback genérico).
  - `_amazon_browser` con `user_data_dir=settings.amazon_user_data_dir`,
    `warmup_amazon_homepage=settings.amazon_warmup_homepage`.
  - `_ml_browser` con `user_data_dir=settings.mercadolibre_user_data_dir`.
- Helpers `get_amazon_browser()`, `get_ml_browser()`. `get_amazon_hunter`
  y `get_amazon_discovery` usan el primero; `get_ml_hunter` y
  `get_ml_discovery` usan el segundo.
- `aclose` cierra los tres.

#### `agents/amazon_hunter_agent.py`

- `HuntOutcome` tiene 6 campos nuevos:
  - `captcha_confidence`, `captcha_should_pause_marketplace`,
    `captcha_strong_signals`, `captcha_visible_signals`,
    `captcha_weak_signals`, `captcha_debug_path`.
- `hunt_one` ahora detecta sospecha medium/low antes de parsear (sin
  necesidad de que `page.blocked=True`), emite
  `runtime_event(amazon_suspected_captcha_form_not_visible)` cuando
  hay form_action sin visible y NO marca el outcome como
  `should_pause_marketplace`.
- Nuevo método `_save_captcha_debug` persiste HTML + screenshot +
  metadatos JSON en `data/debug/amazon_captcha/<ts>_<asin>.{html,png,json}`
  — REGLA 6.

#### `mcp/tools/action_tools.py`

- `_summarize_outcome` ahora incluye los 6 campos de captcha. Sin esto,
  los loops paralelos no podían distinguir confidence high de medium.

#### `browser/amazon_captcha_detector.py`

- `body_tiny_with_title` ya NO sube a `confidence=high` por sí solo. La
  ruta `high` exige estructura fuerte + texto visible humano. Elimina
  el riesgo de marcar como high una página de producto real con title
  `Amazon.com.mx` y body chico (timeout parcial).

#### `browser/playwright_worker.py`

- El log de "Captcha confirmado" ahora imprime
  `strong=, visible=, confidence=, should_pause=` para auditoría inmediata.

#### `scripts/orquestador_ia.py`

- `loop_amazon` reemplaza la suma por string equality. Ahora:
  - cuenta `outcome["captcha_should_pause_marketplace"] is True and
    captcha_confidence == "high"`.
  - pausa **10min sólo con >=2** captchas reales high-confidence.
  - **1** captcha real → backoff corto 60s sin pausa global.
  - 0 reales pero N sospechosos → log y sigue (no pausa).

### Tests añadidos

`tests/unit/agents/test_amazon_captcha_pause_policy.py` (12 tests):

- `test_amazon_captcha_form_action_alone_is_not_pause_worthy`
- `test_amazon_hunter_outcome_carries_captcha_assessment`
- `test_amazon_hunter_outcome_high_confidence_marks_should_pause`
- `test_amazon_captcha_debug_snapshot_saved_for_option3`
- `test_summarize_outcome_exposes_captcha_fields`
- `test_option3_does_not_pause_on_form_action_validate_captcha_alone`
- `test_option3_pauses_only_on_high_confidence_real_captcha`
- `test_option3_one_high_captcha_only_backoff_short`
- `test_no_duplicate_amazon_captcha_detectors_exist`
- `test_amazon_legacy_scrapperia_fixture_not_marked_as_captcha`
- `test_amazon_legacy_scrapperia_search_fixture_not_marked_as_captcha`
- `test_amazon_captcha_check_cli_reports_should_pause_false_for_medium`
- `test_launcher_option3_uses_central_amazon_captcha_detector`

Resultado: **551/551** unit tests passing.

### Verificación contra URLs del log del operador

```
$ python -m ofertas_hunter amazon-captcha-check "https://www.amazon.com.mx/dp/B00DGQMJE0"
strong=['form_action_validate_captcha'] visible=['text_continuar_comprando']
confidence=high should_pause=True
```

Las 5 URLs del log son **captchas reales high-confidence** (form +
visible "Continuar a Compras"). El detector no las clasifica mal: el
problema era que la opción 3 pausaba aunque el detector dijera
`should_pause=False` y aunque solo hubiera 1 captcha. Con el fix:

- Si vuelve a aparecer 1 captcha real → backoff 60s, no pausa.
- Si aparecen 2+ → pausa 10min (correcto).
- Si solo hay form_action sin visible (sospecha) → no pausa.

Para reducir frecuencia de captchas reales, el operador debe correr:

```
python -m ofertas_hunter login --marketplace amazon
```

Y dejar que la sesión persistente Amazon también esté establecida
(actualmente sólo hizo login en ML).
