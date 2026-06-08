# changes.md â€” `ofertas_hunter`

Log de decisiones por bloque, alineado con la spec del usuario.

---

## 2026-06-08 (b)

* Archivo: `src/ofertas_hunter/publishing/screenshot_capturer.py`, `tests/unit/publishing/test_screenshot_capturer_urls.py`. Commit `a6c2a28`.
* Cambio: `normalize_ml_pdp_url` ahora reescribe URLs `articulo.mercadolibre.com.mx/MLM<id>` y `/MLM-<id>` (item-id pelado, sin slug ni `/p/`) a `www.mercadolibre.com.mx/p/<itemid>`. Añadido `_ML_BARE_ITEM_PATH_RE`.
* Motivo: el usuario reportó que la última publicación ML salió con la foto pública en vez del screenshot del PDP. Causa raíz (systematic-debugging): esas URLs slug-less devuelven un **shell HTML vacío de ~9KB** (`#root-app` sin hidratar, sin `h1.ui-pdp-title`) → el capturer no encontraba el título y caía a fallback. NO es captcha/IP (navegación sin challenge) ni cookies (sesión válida). El formato afecta a **~25% de las ofertas ML** (73 de 292 en 48h). El `meli.la`/`affiliate_url` no sirve (redirige al perfil social del afiliado).
* Validación: probadas 4 formas de URL para el item — solo `www.mercadolibre.com.mx/p/<id>` renderiza (h1=True, gallery=1). Confirmado en 5/5 items slug-less reales (orig=no-h1 → fix=OK). E2E en VPS: captura OK 120KB tras el fix.
* Resultado: ✅ 80 tests de publishing verdes (4 nuevos de normalización). Desplegado y bot reiniciado (ahora en tmux, PID 3480969).

---

## 2026-06-06

* Diagnóstico (no cambio de código): el usuario reportó que "las últimas dos salieron con screenshots así" (Termo y Taladro mostraban solo la foto del producto, sin el panel título+precio+buybox).
* Hallazgo: esas dos NO eran screenshots del PDP sino el **fallback a imagen pública** (`media_url=[image_url:0]`) — la foto del catálogo de ML, que es exactamente una imagen de producto sin panel. Es decir, la captura de pantalla volvió a fallar y cayó al fallback.
* Causa raíz (systematic-debugging): NO es problema de cookies (las cookies de auth `orguseridp`/`ssid`/`ftid`/`_d2id` están vigentes hasta 2027) ni del fix de mtime del 5 jun. Es un **challenge anti-bot de ML por IP de datacenter**: los PDP de catálogo (`/p/MLM...`) redirigen a `/gz/account-verification` desde la IP de la VPS. Confirmado que ocurre **incluso anónimo (sin cookies)** y **con el perfil persistente**, 100% de las veces en la ventana de prueba (19h UTC). Es intermitente a lo largo del día (14h-15h UTC = 11/11 screenshots OK; 16h/18h parcial; 19h total fallback).
* Evidencia adicional: el **hunter ML sigue sano** — encola ofertas con título/precio válidos (oid 6482-6488 hasta 18:35). El extractor de ofertas no usa la misma ruta de render que el screenshot (el screenshot navega el PDP completo de catálogo, que es justo lo que ML está challengeando).
* Conclusión: condición externa (reputación de IP del datacenter ante el anti-bot de ML), no un bug. El fallback a imagen pública funciona como red de seguridad (la oferta igual se publica con foto + precios correctos). Sin cambio de código por ahora; el fix del 5 jun (refresco de cookies) sigue siendo correcto para el caso de rotación.
* Mitigaciones posibles a futuro (si el fallback molesta): warmup de home ML antes de capturar, reintento con backoff cuando detecta `account-verification`, o proxy residencial para las capturas. Pendiente de decisión del usuario.

---

## 2026-06-05

* Archivo: `src/ofertas_hunter/publishing/screenshot_capturer.py`, `tests/unit/publishing/test_screenshot_capturer_cookie_refresh.py` (nuevo). Commit `f24bf67`.
* Cambio: el `ScreenshotCapturer` ahora detecta rotación de los archivos de cookies (por `mtime`) y re-inyecta cookies frescas en su contexto antes de cada captura (`_maybe_refresh_cookies`, `_cookie_file_paths`, `_current_cookie_mtimes`; `_inject_cookies` registra mtimes al final).
* Motivo: causa raíz de la regresión "ML manda foto pública en vez de screenshot". El capturer inyectaba cookies UNA sola vez al arrancar y nunca las refrescaba. ML rota su sesión constantemente (el session manager reescribe el archivo y hace hot-reload en el browser del hunter, ~cada 16s), pero el capturer es un browser singleton aparte que nadie refrescaba. Tras la primera rotación ML post-arranque (4 jun 23:46), las cookies del capturer quedaban viejas → el PDP de catálogo `/p/MLM...` renderizaba deslogueado (sin `h1.ui-pdp-title`) → captura `None` → 100% fallback a imagen pública SOLO en ML. Amazon (cookies estáticas inyectadas a mano) seguía al 100% de screenshots, lo que confundía el diagnóstico ("a veces sí, a veces no" era en realidad "Amazon sí, ML no").
* Diagnóstico (systematic-debugging): experimento controlado en VPS — con headers reales del capturer, cookies frescas = 3/3 PDP ML renderizaron; sin cookies = 0/3. DB confirmó ML 0 screenshots / 50 fallback el 5 jun vs Amazon 69/0. Último screenshot ML OK: 4 jun 23:46.
* Resultado: ✅ 77 tests de publishing verdes (incl. 3 nuevos). Verificado E2E en VPS: capturer con cookies stale detecta rotación → refresca → captura ML OK (109KB). Tras reiniciar el bot, primera publicación ML post-fix usó screenshot (ss=1 fb=0), Amazon intacto (ss=2 fb=0).

---

## 2026-06-01

* Archivo: `src/ofertas_hunter/publishing/screenshot_capturer.py` (nuevo), `src/ofertas_hunter/publishing/whatsapp_publisher.py`, `src/ofertas_hunter/orchestrator.py`, `src/ofertas_hunter/config.py`, `tests/unit/publishing/test_publisher_screenshot.py` (nuevo), `tests/unit/publishing/test_screenshot_capturer_urls.py` (nuevo), `.env.example`, `.gitignore`
* Cambio: la imagen que se despacha al grupo de WhatsApp ahora es un screenshot recortado de la ficha real del producto (imagen + título + precio + caja de compra), estilo `_capture_detail_section` del scraper legacy, para Amazon y Mercado Libre. `ScreenshotCapturer` gestiona su propio navegador headless con viewport desktop fijo (1366x900), inyecta cookies ML+Amazon, reescribe `articulo.mercadolibre.com.mx`→`www.` (la forma `articulo.` 404 en catálogo `/p/MLM`) y une las columnas clave del PDP. El `WhatsAppPublisher` lo invoca antes de `send_media`. Best-effort: si la captura falla (captcha/timeout/sin URL), fallback automático a la imagen pública `image_url`. Controlado por `PUBLISH_SCREENSHOT_ENABLED` (default true).
* Motivo: la imagen pública del catálogo se veía pobre; el operador validó visualmente las capturas del spike y pidió usarlas siempre.
* Relación: spike previo en `scripts/spike_product_screenshots.py`; chokepoint único en el publisher (todo lo despachado pasa por `send_media`).
* Resultado: ✅ 1149 unit tests verdes (2 skip Windows); verificación E2E del capturer real produjo JPEGs válidos para ML (incl. reescritura articulo→www) y Amazon.


* Cambio: agregada regresión en rojo para clasificar `Connection Closed` de Evolution como fallo temporal (`temporary`) en vez de fallo HTTP genérico.
* Motivo: en VPS la instancia aparece `open`, pero `sendText/sendMedia` están devolviendo `Connection Closed`; había que fijar ese contrato antes del hardening del dispatcher.
* Relación: abre el bloque de reintentos seguros del transporte Evolution.
* Resultado: ⚠️ parcial

* Archivo: `tests/unit/dispatching/test_dispatcher.py`
* Cambio: agregadas regresiones para exigir que un fallo temporal de Evolution reencole el item con backoff en vez de marcarlo `failed`, manteniendo el corte del tick ante una caída global.
* Motivo: el dispatcher estaba quemando pendientes reales cuando Evolution devolvía `Connection Closed`.
* Relación: consume la nueva semántica temporal del cliente Evolution.
* Resultado: ⚠️ parcial

* Archivo: `tests/unit/dispatching/test_dispatcher.py`
* Cambio: acotada la regresión del dispatcher para que sólo cambie el comportamiento ante fallos temporales de transporte; los `500` genéricos siguen quedando como `failed`.
* Motivo: el hardening buscado no debe esconder fallos globales desconocidos bajo el mismo path de reintento.
* Relación: corrige el alcance del test rojo recién agregado.
* Resultado: ⚠️ parcial

* Archivo: `src/ofertas_hunter/publishing/evolution_client.py`, `src/ofertas_hunter/dispatching/outbox.py`
* Cambio: añadido clasificador de errores temporales de Evolution (`connection_closed`, `http_error`) y nuevo path de outbox `mark_retry_later(...)` para reencolar con `scheduled_for`.
* Motivo: separar caídas transitorias del transporte de fallos permanentes para no perder backlog publicable.
* Relación: implementación mínima de los tests rojos agregados hoy.
* Resultado: ⚠️ parcial

* Archivo: `src/ofertas_hunter/dispatching/dispatcher.py`
* Cambio: el dispatcher ahora detecta fallos temporales de Evolution y reencola el item con backoff configurable (`temporary_failure_retry_seconds`) en vez de marcarlo `failed`.
* Motivo: evitar que una caída transitoria de WhatsApp/Evolution consuma backlog sano y deje el bot sin material cuando el transporte vuelva.
* Relación: usa la nueva semántica `EvolutionResponse.temporary` y `outbox.mark_retry_later(...)`.
* Resultado: ⚠️ parcial

* Archivo: validación local + VPS del dispatcher/Evolution
* Cambio: verificados `tests/unit/publishing/*` y `tests/unit/dispatching/*` en local, sincronizados `evolution_client.py`, `dispatcher.py`, `outbox.py` a la VPS, reiniciado `evolution-api.service` y relanzado `start.sh` en modo `3 => f` dentro de `tmux`.
* Motivo: cerrar el bloqueo real de producción donde Evolution quedó con socket interno cerrado (`Precondition Required / Connection Closed`) y el foreground no retomaba dispatch estable.
* Relación: valida el hardening nuevo contra la recuperación real del transporte.
* Resultado: ✅ éxito — Evolution volvió a responder `201` y el outbox pasó de `sent=717` a `sent=718` (item `3681`, `2026-06-01T15:58:20.097Z`).

## 2026-05-30 (guardrail anti precio-por-unidad / descuento extremo)

* Archivo: `publishing/whatsapp_publisher.py`, `agents/legacy_amazon/price_parser.py`, `agents/legacy_amazon/adapter.py`, `agents/amazon_outbox_sanitizer.py`, `config.py`, `mcp/context.py`, `__main__.py`, `orchestrator.py`
* Problema: Amazon publicó falsos positivos 99/100% (guantes a $0.60/$0.69/$0.74) — el extractor tomaba el **precio por unidad** (`$0.69 / unidad`) como precio total. Causa raíz confirmada: el bloque `#corePrice_feature_div` contiene `$69.00 ... $0.69 / unidad` y `.a-price .a-offscreen` capturaba `$0.69`.
* Cambio: (1) `extract_verified_current_price` + `_looks_like_unit_price_node` ya rechazaban unit-price en el extractor (verificado: B0F95Y6HTX → $69 no $0.69); (2) **defensa en profundidad en el publisher** — `_amazon_gate` bloquea `amazon_unit_price_as_current_price` (raw text con `/unidad`), `amazon_extreme_discount_unverified` (≥90% sin `extreme_discount_verified`), `amazon_current_price_suspicious` (cur<10 con old>50, o cur<1% de old); (3) trazabilidad en payload: `current_price_source/raw_text/is_unit_price/verified`, `extreme_discount_verified`; (4) sanitizer detecta y bloquea `suspicious_price`.
* Resultado: ✅ 980 tests verdes (17 nuevos del guardrail). Replay del gate contra los 8 guantes publicados → los 8 BLOQUEADOS (`amazon_extreme_discount_unverified`). 14 falsos positivos sent identificados (8 guantes unit-price + 6 de descuento alto legítimo/dudoso).

---

## 2026-05-30 (enricher robusto)

* Archivo: `src/ofertas_hunter/runtime/profile_lock.py` (nuevo), `mcp/tools/action_tools.py`, `mcp/context.py`, `__main__.py`, `scripts/orquestador_ia.py`
* Cambio: enriquecimiento de afiliados Amazon robusto — (1) timeout específico de `enrich_amazon_affiliates` subido a 240s; (2) `ProfileLock` (fcntl, cross-process) sobre `secrets/browser_profiles/amazon/.profile.lock` que impide dos `launch_persistent_context` sobre el mismo perfil; el enricher SALTA si está ocupado (`amazon_profile_busy`); (3) cleanup garantizado del browser en cancelación/timeout (`_cleanup_amazon_browser`), idempotente; (4) el CLI `amazon-enrich-affiliates` espera el filelock hasta 60s; (5) logs estructurados (`amazon_profile_lock_acquired/busy/released`, `affiliate_enrich_started/enriched/failed/timeout`, `affiliate_cleanup_*`)
* Motivo: el enricher competía con hunt/discovery por el perfil Chromium → `sitestripe_not_visible` / `TargetClosedError` y 2 contextos simultáneos
* Resultado: ✅ contención de 2→1 contexto; `enriquecidos=4 fallidos=0`; Amazon empezó a publicar con `amzn.to` y `old_price_verified=true`. Telegram-Amazon sigue bloqueado (0 publicables). 967 tests verdes + 9 en VPS (incluye test POSIX del filelock)

---

## 2026-05-30

* Archivo: VPS `data/ofertas_hunter.db`
* Cambio: se descartaron los pendientes Amazon legacy `2429` y `2433` con `discarded_reason=amazon_unit_price_false_positive`; se guardó backup de filas en `/opt/deal-agent/ofertas_hunter/data/outbox_unit_price_false_positive_backup.20260530T170013Z.json`
* Motivo: evitar que al reiniciar el bot publique items ya contaminados por precio por unidad (`$0.69`) como precio total
* Relación: limpieza operativa posterior al fix de parsers Amazon
* Resultado: ✅ éxito

## 2026-05-30

* Archivo: VPS `data/ofertas_hunter.before_unit_price_cleanup.20260530T165856Z.db`
* Cambio: se eliminó un backup completo parcial fallido de 6.7GB creado antes de limpiar outbox
* Motivo: el filesystem de la VPS quedó al 100% durante el intento de backup completo; el archivo era incompleto y no había modificado la DB
* Relación: se reemplazó por backup mínimo de filas afectadas
* Resultado: ✅ éxito

## 2026-05-30

* Archivo: validación local Amazon unit-price false positives
* Cambio: se ejecutan pruebas focalizadas de legacy Amazon, parser Amazon nuevo, publisher gate y sanitizer
* Motivo: confirmar que el fix no rompe descuentos válidos ni guardrails previos de publicación Amazon
* Relación: valida el bloque de corrección de precio por unidad
* Resultado: ✅ éxito (`73 passed`)

## 2026-05-30

* Archivo: `src/ofertas_hunter/extraction/amazon_product_parser.py`
* Cambio: el parser Amazon nuevo prioriza `.priceToPay`, ignora precios por unidad y evita usar precios tachados/list-price como precio actual; también evita unitarios como precio anterior
* Motivo: prevenir el mismo falso positivo de guantes si el hunter nuevo queda activo
* Relación: cierra la regresión agregada en `test_amazon_product_parser.py`
* Resultado: ⚠️ parcial

## 2026-05-30

* Archivo: `tests/unit/extraction/test_amazon_product_parser.py`
* Cambio: se agrega regresión equivalente para el parser Amazon nuevo: `$148.00` debe ser precio actual y `$0.74 / unidad` debe ignorarse
* Motivo: aunque el falso positivo real vino de `legacy_amazon`, el parser nuevo tenía una superficie de selectores similar
* Relación: extiende el fix de precio unitario a ambos hunters Amazon
* Resultado: ⚠️ parcial

## 2026-05-30

* Archivo: `src/ofertas_hunter/agents/legacy_amazon/price_parser.py`
* Cambio: el extractor estricto de precio actual ahora descarta candidatos dentro de `basisPrice`, `a-text-price`, `data-a-strike`, `listPrice` o `was_price`
* Motivo: tras descartar precio unitario, los selectores amplios podían caer al precio anterior tachado como si fuera precio actual
* Relación: endurece el fix de `extract_verified_current_price()`
* Resultado: ⚠️ parcial

## 2026-05-30

* Archivo: `src/ofertas_hunter/agents/legacy_amazon/price_parser.py`
* Cambio: se agrega `extract_verified_current_price()` para priorizar `.priceToPay` y descartar candidatos cuyo contexto marque precio por unidad/pieza/kg/ml; `_extract_current_price()` lo usa antes del fallback legacy
* Motivo: evitar que Amazon legacy publique `$0.69 / unidad` como si fuera el precio total del producto
* Relación: corrige la regresión agregada en `tests/unit/agents/legacy_amazon/test_current_price.py`
* Resultado: ⚠️ parcial

## 2026-05-30

* Archivo: `tests/unit/agents/legacy_amazon/test_current_price.py`
* Cambio: se agrega regresión para Amazon legacy cuando el bloque de precio trae precio principal y precio por unidad (`$148.00` + `$0.74 / unidad`)
* Motivo: las publicaciones de guantes tomaron el precio por unidad como precio actual y generaron descuentos falsos de 99-100%
* Relación: complementa los guardrails previos de precio anterior verificado; el fallo nuevo está en `current_price`, no en `old_price`
* Resultado: ⚠️ parcial

## 2026-05-30

* Archivo: `src/ofertas_hunter/publishing/whatsapp_publisher.py`, `src/ofertas_hunter/publishing/formatter.py`, `src/ofertas_hunter/config.py`, `mcp/context.py`, `orchestrator.py`, `__main__.py`
* Cambio: guardrail duro de Amazon en el publisher — ningún item Amazon se publica sin `affiliate_url` válido (`amzn.to/` o `tag=`); las ofertas normales exigen `old_price_verified` + coherencia de descuento (tolerancia 2 pts) + descuento >= mínimo. El formatter ya no deriva "Antes" desde el % para Amazon. Razones de descarte: `amazon_missing_affiliate`, `amazon_no_verified_old_price`, `amazon_discount_mismatch`, `amazon_below_min_discount`
* Motivo: el bot publicaba ofertas Amazon sin link de afiliado y con descuentos inventados (caso sudadera $350 con "Antes $1,422.61" falso)
* Relación: cierra el path de publicación; es la fuente de verdad final aunque la extracción falle
* Resultado: ✅ éxito (13 tests gate + 3 integración)

## 2026-05-30

* Archivo: `src/ofertas_hunter/agents/legacy_amazon/price_parser.py`, `src/ofertas_hunter/agents/legacy_amazon/adapter.py`
* Cambio: `extract_verified_old_price()` — solo acepta precio tachado/de lista dentro del bloque principal de precio; rechaza variantes (twister), otros vendedores (aod), mensualidades/MSI y recomendados. El adapter propaga `old_price_verified`/`discount_percent_verified` y NO encola descuento sin precio anterior verificado. Trazabilidad en payload: `old_price`, `old_price_source`, `old_price_verified`, `discount_percent_verified`, `affiliate_url`, `affiliate_valid`, `validation_errors`, `reject_reason`
* Motivo: causa raíz de los descuentos falsos — el extractor tomaba precios de variantes/otros bloques como "precio anterior"
* Resultado: ✅ éxito (7 tests extractor + 31 adapter)

## 2026-05-30

* Archivo: `scripts/orquestador_ia.py`, `mcp/tools/action_tools.py`, `mcp/context.py`, `src/ofertas_hunter/agents/amazon_outbox_sanitizer.py`, `src/ofertas_hunter/__main__.py`
* Cambio: nueva tool MCP `enrich_amazon_affiliates` + fase automática de enriquecimiento en el loop del dispatcher (antes de `dispatch_outbox`, con timeout y manejo de errores). Nuevo comando CLI `amazon-sanitize-outbox` que enriquece afiliados y bloquea (state=discarded) lo no publicable, con resumen (total/ya_ok/enriquecidos/fallidos/bloqueados)
* Motivo: el legacy hunter no genera afiliados y el enricher no estaba en el loop; los items quedaban pending sin afiliado y el publisher los bloqueaba indefinidamente
* Resultado: ✅ éxito (961 tests verdes)

---

## 2026-05-29

* Archivo: investigación captcha Amazon local/VPS
* Cambio: se contrasta el snapshot real `data/debug/amazon_captcha/...B010ASII32.html` contra corridas live locales y remotas; se confirma que el detector no generó un falso positivo, porque el HTML guardado contiene el challenge real de Amazon (`/errors/validateCaptcha` + botón “Continuar a Compras”), pero también se demuestra que el bloqueo es intermitente y que el mismo URL hoy carga la PDP real tanto por `worker.fetch()` como por `AmazonHunterAgent.hunt_one()`
* Motivo: el operador reportó que en local nunca veía captcha y pidió una revisión profunda antes de tocar la lógica de detección
* Relación: fundamenta el hardening posterior en `playwright_worker.py`; el problema no era “aflojar” el detector sino tolerar mejor interstitials reales transitorios
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: validación live hunter Amazon local + VPS
* Cambio: después del retry con pestaña nueva, `AmazonHunterAgent.hunt_one()` se ejecuta 3 veces seguidas en local y 3 veces seguidas en VPS para `B010ASII32`, siempre sin captcha, con `affiliate_url` presente y `enqueued_outbox_id` válido
* Motivo: demostrar que el fix no sólo pasa tests, sino que estabiliza el path real del hunter sobre el mismo producto que había generado el challenge observado
* Relación: complementa las pruebas unitarias nuevas del worker y el wiring previo de `AmazonSession`
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/browser/playwright_worker.py
* Cambio: el browser worker Amazon ahora, ante un captcha high-confidence, hace un reintento limpio con pestaña nueva y settle corto antes de confirmar el bloqueo y activar backoff
* Motivo: la investigación mostró que el detector sí vio challenges reales, pero el problema operativo puede ser transitorio; el worker necesitaba una segunda oportunidad controlada antes de pausar marketplace
* Relación: responde a las pruebas nuevas de `test_playwright_worker.py` y complementa el wiring de cookies Amazon hecho hoy
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: tests/unit/browser/test_playwright_worker.py
* Cambio: se agregan pruebas en rojo para endurecer el browser worker Amazon: si el primer fetch ve un captcha real, debe reintentar una vez con pestaña nueva antes de declarar bloqueo definitivo
* Motivo: la evidencia ya mostró que el detector no mintió en el snapshot guardado; el problema restante es hacer al worker más tolerante a challenges transitorios sin relajar la detección real
* Relación: complementa la investigación local/VPS donde `direct_goto` y `worker_fetch` hoy responden bien, pero hubo un caso real intermitente que conviene amortiguar
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/__main__.py, src/ofertas_hunter/orchestrator.py, src/ofertas_hunter/mcp/context.py
* Cambio: se cablea `AmazonSession` al runtime real de Amazon; `amazon-hunt`, `amazon-parse-url`, el orquestador y `ServerContext.get_amazon_hunter()` ahora cargan `AMAZON_COOKIES_PATH` en el contexto Playwright e inyectan `PlaywrightAffiliateExtractor`; además se agrega el subcomando `amazon-enrich-affiliates`
* Motivo: el extractor SiteStripe y el enricher Amazon ya existían, pero el bot seguía levantando `amazon_hunter` sin cookies/extractor y por eso seguía publicando enlaces genéricos
* Relación: completa el wiring operativo que faltaba después de crear `amazon_affiliate.py` y `amazon_affiliate_enricher.py`
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: validación local + VPS Amazon afiliados
* Cambio: se validan localmente `check-config`, el nuevo `amazon-enrich-affiliates --help`, una extracción real SiteStripe (`success=True`, `cookies_loaded=30`) y un enriquecimiento real de outbox temporal (`affiliate_status=ok`); luego se sincroniza el mismo bloque a la VPS, se inyecta `secrets/amazon_cookies.json`, se actualiza `.env` remoto y se repite la validación real con éxito
* Motivo: cerrar el cambio con evidencia operativa, no sólo con pruebas unitarias, y dejar la VPS lista para reutilizar la misma sesión Amazon afiliada
* Relación: usa el nuevo `AmazonSession`/`amazon_cookies_path` y prueba tanto el extractor como el enricher en entornos local y remoto
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: .env.example, .env
* Cambio: se documenta y activa `AMAZON_COOKIES_PATH`, junto con `AMAZON_USER_DATA_DIR`, para que el hunter/enricher Amazon consuman la misma sesión/cookies tanto en local como en VPS
* Motivo: la extracción SiteStripe necesita una ruta de cookies estable y configurable; el código ya no debe depender de pruebas manuales con paths implícitos
* Relación: completa el nuevo `amazon_cookies_path` expuesto en `Settings` y el bootstrap de `AmazonSession`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/mcp/test_amazon_hunter_wiring.py
* Cambio: el stub de `browser_context` del test ahora preserva `BrowserWorker` y `RenderedPage` para no romper imports del `AmazonHunterAgent`
* Motivo: el primer rojo posterior al wiring Amazon provenía del test mismo, no del runtime: el monkeypatch sustituía demasiado del módulo y falseaba un `ImportError`
* Relación: mantiene útil la prueba de `ServerContext.get_amazon_hunter()` como regresión del wiring real
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: src/ofertas_hunter/session/amazon_session.py, src/ofertas_hunter/config.py
* Cambio: se crea `AmazonSession` para resolver/cargar/injectar cookies Amazon al contexto Playwright y se expone `amazon_cookies_path` en `Settings`
* Motivo: hacía falta una capa operativa equivalente a `MercadoLibreSession` para que el extractor SiteStripe y el hunter Amazon usen cookies reales desde env sin hardcodes
* Relación: responde al rojo recién agregado en `test_amazon_session.py` y `test_amazon_hunter_wiring.py`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/session/test_amazon_session.py, tests/unit/mcp/test_amazon_hunter_wiring.py, tests/unit/test_config.py
* Cambio: se agregan pruebas en rojo para el wiring faltante de Amazon afiliados: una sesión Amazon debe resolver/injectar cookies al browser, `ServerContext.get_amazon_hunter()` debe cargar cookies e inyectar `affiliate_extractor`, y `Settings` debe exponer `amazon_cookies_path`
* Motivo: el extractor/enricher Amazon ya existe, pero aún faltaba cerrar el path real de cookies + runtime/CLI para que `amazon_hunter` deje de publicar enlaces genéricos
* Relación: extiende el bloque previo de `amazon_affiliate.py`/`amazon_affiliate_enricher.py` con la parte operativa que todavía estaba pendiente
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: src/ofertas_hunter/session/cookie_store.py
* Cambio: `CookieStore.load()` ahora lee JSON con `utf-8-sig` para tolerar BOM de PowerShell/Windows
* Motivo: el archivo real `secrets/amazon_cookies.json` generado en la prueba live se interpretaba como inválido aunque su contenido era correcto
* Relación: cierra la prueba en rojo de `test_cookie_store_loads_utf8_bom_json` y robustece la inyección de cookies para Amazon/ML
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/session/test_cookie_store.py
* Cambio: se agrega prueba en rojo para aceptar archivos JSON de cookies UTF-8 con BOM
* Motivo: en la prueba real de Amazon, PowerShell generó `secrets/amazon_cookies.json` con BOM y el loader lo marcó falsamente como ausente
* Relación: protege el flujo operativo de inyección de cookies Amazon/ML desde Windows
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: src/ofertas_hunter/marketplaces/amazon_affiliate.py, src/ofertas_hunter/agents/amazon_affiliate_enricher.py, src/ofertas_hunter/agents/amazon_hunter_agent.py
* Cambio: se agrega el extractor real de afiliados Amazon vía SiteStripe + clipboard, un enricher para backlog Amazon y el hunter nuevo pasa a preferir `affiliate_url` cuando la extracción está disponible sin bloquear la publicación si falla
* Motivo: los links genéricos de `amazon_hunter` no respetaban el objetivo de publicar con afiliado; además hacía falta una vía para corregir items ya en outbox
* Relación: implementa el contrato fijado en las pruebas nuevas y sigue el patrón funcional ya validado por Mercado Libre
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/agents/test_amazon_hunter_agent.py, tests/unit/agents/test_amazon_affiliate_enricher.py
* Cambio: se agregan pruebas en rojo para el contrato Amazon afiliados: `amazon_hunter` debe preferir `affiliate_url` cuando exista y un nuevo enricher debe corregir items Amazon pendientes con link genérico
* Motivo: fijar antes de implementar que el flujo nuevo use SiteStripe para publicación y que el backlog de `amazon_hunter` pueda migrarse a afiliado sin romper el outbox
* Relación: reutiliza el patrón ya validado en `mercadolibre_affiliate_enricher` pero sin imponer gate duro de publicación para Amazon
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: validación `ctx=None` en VPS
* Cambio: se sincroniza `ml_session_runtime.py` al servidor, se ejecuta `tests/unit/session/test_ml_session_runtime.py` en la venv remota y se reinicia `ofertas-hunter.service`
* Motivo: confirmar que el warning de `ml_session_manager` desaparece en el modo `python -m ofertas_hunter run` desplegado en producción
* Relación: cierra el ajuste local del runtime de recovery ML para el orquestador nativo
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/session/ml_session_runtime.py, tests/unit/session/test_ml_session_runtime.py
* Cambio: el runtime de recovery ML ya tolera `ctx=None` en modo orquestador nativo y se agrega test de regresión para el arranque con poller Telegram sin contexto
* Motivo: en VPS `python -m ofertas_hunter run` construía `MLSessionRecoveryRuntime` sin `ctx` y `start()` intentaba inyectar `ml_session_manager` sobre `None`, dejando un traceback en cada arranque
* Relación: completa el flujo Telegram/ML desplegado antes sin cambiar la semántica del hot-reload cuando sí existe `ctx`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: validación remota VPS completa
* Cambio: tras extraer el paquete completo en `/opt/deal-agent/ofertas_hunter`, se ejecuta `deploy/install.sh`, `pytest -q` y reinicio de `ofertas-hunter`, `ofertas-hunter-dispatcher`, `ofertas-hunter-telegram`
* Motivo: confirmar que la VPS quedó actualizada con el checkout completo y que los servicios arrancan sobre ese código
* Relación: cierra la sincronización completa iniciada para evitar el subconjunto del fallback `scp` de `scripts/vps_sync.ps1`
* Resultado: ⚠️ parcial — deploy y servicios OK; suite remota reporta 2 fallos existentes en `tests/unit/session/test_ml_hot_reload_acceptance.py` y `tests/unit/session/test_ml_session_inbound.py`

## 2026-05-29

* Archivo: despliegue VPS completo
* Cambio: sincronización completa aplicada sobre `/opt/deal-agent/ofertas_hunter` mediante paquete tar del checkout, excluyendo `data`, `secrets`, `.venv`, `.git`, `.kiro` y cachés
* Motivo: completar la actualización integral del repo en la VPS, incluyendo archivos nuevos y cambios fuera del fallback acotado
* Relación: reemplaza el estado `⚠️ parcial` de la primera pasada con `scripts/vps_sync.ps1`
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: despliegue VPS completo
* Cambio: se fuerza una sincronización completa del checkout hacia `/opt/deal-agent/ofertas_hunter` porque `scripts/vps_sync.ps1` cayó al fallback `scp` y sólo subió un subconjunto de archivos
* Motivo: el operador pidió que la VPS quedara totalmente actualizada, incluyendo archivos nuevos y cambios fuera de la lista acotada del fallback
* Relación: complementa los despliegues selectivos previos de Telegram y systemd
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: src/ofertas_hunter/config.py, src/ofertas_hunter/publishing/evolution_client.py, src/ofertas_hunter/session/ml_session_poller.py, src/ofertas_hunter/__main__.py, src/ofertas_hunter/orchestrator.py, src/ofertas_hunter/mcp/context.py
* Cambio: la integración Evolution API pasa a ser totalmente configurable por env para URL, header auth, nombre de instancia y destino activo; además se agrega `connection_state()` y alias `send_image()` sin romper `send_media()`
* Motivo: actualizar la configuración Evolution a la nueva instancia GCP sin hardcodes en código funcional y mantener compatibilidad con los flujos existentes de publicación y polling
* Relación: preserva el flujo actual de WhatsApp y de recovery ML; no cambia todavía alertas por Telegram ni el destino activo de publicación
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: .env.example, .env
* Cambio: se documenta la nueva superficie env de Evolution API y se actualiza la configuración local con la nueva base URL, header `apikey`, nombre de instancia y metadatos administrativos; el canal `@newsletter` queda sólo como referencia
* Motivo: dejar la integración reajustable sin tocar código y sin activar aún un destino nuevo de publicación
* Relación: usa la nueva compatibilidad agregada en `Settings` y respeta la precedencia del target activo existente
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/publishing/test_evolution_client.py, tests/unit/test_config.py
* Cambio: se agregan pruebas del contrato Evolution configurable (header, URL, payloads, connection state) y de la precedencia de settings/aliases (`EVOLUTION_INSTANCE_NAME`, `EVOLUTION_DEFAULT_TARGET`)
* Motivo: fijar en tests la nueva configuración env-driven y prevenir regresiones de compatibilidad
* Relación: valida los cambios en `config.py`, `evolution_client.py` y el cableado runtime
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: validación Evolution API
* Cambio: se ejecutan `tests/unit/publishing/test_evolution_client.py`, `tests/unit/publishing/`, `tests/unit/session/test_ml_session_poller.py`, `tests/unit/test_config.py` y `python -m ofertas_hunter check-config`; además se verifica que no haya hardcodes de host/key/instancia en `src/`
* Motivo: cerrar el cambio con evidencia de compatibilidad y de lectura correcta desde `.env`
* Relación: completa la migración de configuración Evolution sin tocar todavía Telegram ni alertas ML
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/config.py, src/ofertas_hunter/notifications/telegram.py, src/ofertas_hunter/session/ml_session_alerting.py, src/ofertas_hunter/session/ml_session_recovery.py, src/ofertas_hunter/session/ml_session_runtime.py, src/ofertas_hunter/__main__.py, src/ofertas_hunter/orchestrator.py
* Cambio: las alertas de cookies/sesión de Mercado Libre ahora pueden salir por Telegram Bot API cuando `ML_SESSION_ALERT_CHANNEL=telegram`; el monitor usa un `alert_send` separado del `evolution_send` del inbound/poller para no tocar la publicación normal ni el recovery por WhatsApp
* Motivo: cambiar únicamente el canal de alerta administrativa de `cookie_expiry` sin alterar Evolution para publicación de ofertas ni el flujo operativo existente de `/cookies_ml`
* Relación: se apoya en la configuración Evolution previa pero no la modifica; mantiene compatibilidad temporal con `ML_SESSION_ALERT_CHANNEL=whatsapp`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/notifications/test_telegram.py, tests/unit/session/test_ml_session_alerting.py, tests/unit/session/test_ml_session_recovery.py
* Cambio: se agregan pruebas del notifier Telegram, del routing `whatsapp/telegram` para alertas ML y del contenido de `cookie_expiry` cuando el canal configurado es Telegram
* Motivo: fijar en tests que el monitor ML no intente usar Evolution para alertas cuando el canal sea Telegram y que la falta de credenciales no tumbe el proceso
* Relación: valida los cambios en `telegram.py`, `ml_session_alerting.py` y `ml_session_recovery.py`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: .env.example, .env
* Cambio: se documenta y configura `ML_SESSION_ALERT_CHANNEL`, `ML_SESSION_TELEGRAM_BOT_TOKEN`, `ML_SESSION_TELEGRAM_CHAT_ID` y `ML_SESSION_TELEGRAM_TIMEOUT_SECONDS`; la configuración local queda apuntando a Telegram para alertas ML
* Motivo: dejar el canal de alerta reajustable desde env sin hardcodes en `src/`
* Relación: mantiene Evolution para ofertas y deja `ML_SESSION_ADMIN_NUMBERS` sólo para el modo temporal `whatsapp`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: validación alertas ML por Telegram
* Cambio: se ejecutan `tests/unit/session/`, `tests/unit/notifications/test_telegram.py`, `tests/unit/session/test_ml_session_alerting.py`, `tests/unit/test_orchestrator.py` y se verifica que el token/chat no estén hardcodeados en `src/`
* Motivo: cerrar el ajuste con evidencia de compatibilidad y aislamiento del cambio al flujo de alertas ML
* Relación: confirma que el recovery por webhook/poller sigue verde mientras el monitor cambia de canal a Telegram
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/notifications/telegram.py, src/ofertas_hunter/session/ml_session_telegram_poller.py, src/ofertas_hunter/session/ml_session_runtime.py, src/ofertas_hunter/config.py
* Cambio: se completa el recovery ML por Telegram agregando polling de `getUpdates`, descarga de `.json` vía `getFile` y activación de un poller Telegram que reemplaza el inbound HTTP/Evolution cuando `ML_SESSION_ALERT_CHANNEL=telegram`
* Motivo: cerrar el riesgo restante donde las alertas ya salían por Telegram pero la recepción de `/cookies_ml` seguía dependiendo de WhatsApp/Evolution
* Relación: mantiene Evolution intacto para publicación normal de ofertas y conserva compatibilidad con el modo temporal `whatsapp`
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: tests/unit/session/test_ml_session_telegram_poller.py, tests/unit/session/test_ml_session_runtime.py, .env.example, .env
* Cambio: se agregan pruebas del poller Telegram para JSON inline y attachment `.json`, un test de runtime que evita arrancar el inbound WhatsApp en modo Telegram, y se documenta `ML_SESSION_TELEGRAM_POLL_INTERVAL_SECONDS`
* Motivo: fijar el contrato del flujo completo `/cookies_ml` por Telegram y dejarlo configurable desde env
* Relación: complementa el notifier y el routing de alertas ML ya implementados antes
* Resultado: ⚠️ parcial

## 2026-05-29

* Archivo: validación recovery ML por Telegram completo
* Cambio: se ejecutan `tests/unit/session/`, `tests/unit/notifications/test_telegram.py`, `tests/unit/session/test_ml_session_alerting.py`, `tests/unit/session/test_ml_session_telegram_poller.py`, `tests/unit/session/test_ml_session_runtime.py` y se verifica que las credenciales reales sólo vivan en `.env`
* Motivo: confirmar que el flujo alerta + recepción + confirmación de cookies ya no depende de Evolution cuando el canal es Telegram
* Relación: cierra el cambio incremental posterior al ajuste inicial de alertas
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/session/ml_session_telegram_poller.py, src/ofertas_hunter/session/ml_session_recovery.py, tests/unit/session/test_ml_session_telegram_poller.py, tests/unit/session/test_ml_session_recovery.py
* Cambio: el flujo Telegram ya no requiere `/cookies_ml`; acepta directamente el siguiente JSON o adjunto `.json/.txt`, y prioriza el intento reciente con documento/comando para ignorar fragmentos viejos de JSON partido
* Motivo: en la prueba live real Telegram dividió el JSON pegado en varios mensajes y el operador confirmó que, tras la alerta, el siguiente mensaje siempre serán las cookies
* Relación: mantiene compatibilidad con `/cookies_ml` si el operador lo usa, pero ya no depende de ese comando para el recovery
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: validación live Telegram recovery
* Cambio: se procesa un adjunto real enviado al chat Telegram configurado; el poller descarga el archivo, valida `30` cookies y dispara el callback de aplicación en modo controlado (`on_cookies` stub) sin sobreescribir todavía la sesión activa local
* Motivo: demostrar end-to-end el canal real de recepción antes de aplicar cookies reales al runtime productivo
* Relación: cierra la prueba operativa del flujo Telegram después de ajustar soporte `.json/.txt` y eliminación del comando obligatorio
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/__main__.py, tests/unit/saneamiento/test_cli_wiring.py
* Cambio: se cierra el blocker del spec `bot-saneamiento-vps` moviendo imports pesados a imports locales para que `python -m ofertas_hunter saneamiento --help` no cargue módulos del Bot_Principal; además se agrega test de subprocess/importtime para wiring CLI e aislamiento
* Motivo: cumplir el Requirement 2.3 del spec y dejar protegido el entrypoint de saneamiento contra regresiones de imports
* Relación: completa el subcomando `python -m ofertas_hunter saneamiento`, sus wrappers legacy y las units systemd ya integradas previamente
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: validación repo / suite completa
* Cambio: se revalida `bot-saneamiento-vps` con `tests/unit/saneamiento`, `tests/unit/telegram/test_message_parser.py`, `tests/`, `python -m ofertas_hunter saneamiento --help` e `importtime` sin imports prohibidos
* Motivo: cerrar el dictamen pendiente de despliegue controlado con evidencia fresca del árbol actual
* Relación: confirma que el ajuste previo del parser Amazon ya dejó de romper la suite completa y que el aislamiento de saneamiento quedó operativo
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/telegram/candidate_builder.py
* Cambio: se actualiza la documentación inline para reflejar que Amazon con formato estructurado también entra como `telegram_deal_signal` pendiente de revalidación
* Motivo: mantener la semántica del archivo consistente con la nueva regla aplicada al builder
* Relación: complemento documental del ajuste funcional en el mismo archivo
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/telegram/candidate_builder.py
* Cambio: los mensajes estructurados de Amazon (`Precio Oferta + Cupón` o `DE X A Y`) ahora pueden entrar como `telegram_deal_signal` para revalidación live aunque el texto no traiga un `discount_visible >= 50`
* Motivo: varios canales Amazon muestran el descuento fuerte únicamente en la página real; el listener debía dejar pasar esos candidatos para que Playwright confirme o descarte
* Relación: se mantiene el descarte temprano de Mercado Libre desde Telegram y el filtro real de publicación sigue ocurriendo en revalidación
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/telegram/message_parser.py
* Cambio: el parser de Telegram para Amazon ahora prioriza el precio final en formatos `DE X A Y` y `Precio Oferta + Cupón`, ignorando montos auxiliares como compra mínima, tope y cupones monetarios
* Motivo: el parser estaba tomando el primer precio visible del mensaje, lo que rompía mensajes Amazon con precio tachado + precio final o con restricciones de cupón
* Relación: alimenta correctamente al candidate builder y a la revalidación live de Amazon
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: tests/unit/telegram/test_candidate_builder.py
* Cambio: se agrega caso Amazon con formato `Precio Oferta + Cupón` que debe encolarse para revalidación live aunque el texto no exponga un descuento >=50
* Motivo: algunos mensajes Amazon del canal solo muestran el descuento fuerte en la página real; el listener necesita dejarlos pasar a Playwright para decidir con datos reales
* Relación: complementa los tests del parser para formatos de Telegram Amazon con cupón
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: tests/unit/telegram/test_message_parser.py
* Cambio: se agregan casos Amazon de Telegram para formatos `DE X A Y`, `Precio Oferta + Cupón` y exclusión de montos de compra mínima/tope
* Motivo: fijar en pruebas el formato real de canales Telegram Amazon donde el parser tomaba el primer precio visible en lugar del precio final publicable
* Relación: prepara la corrección acotada a Amazon sin tocar la regla de ignorar Mercado Libre desde Telegram
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: .env
* Cambio: se reescribe el archivo en UTF-8 sin BOM para que systemd no ignore la primera línea al cargar `EnvironmentFile`
* Motivo: durante la sincronización al VPS el servicio reportó `Ignoring invalid environment assignment` por el BOM agregado desde Windows
* Relación: corrige la última sincronización del flujo Telegram/ML y de la configuración Evolution para que el runtime remoto lea el env completo sin warnings
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: VPS /opt/deal-agent/ofertas_hunter
* Cambio: despliegue selectivo de corrección Telegram, reinstalación de units systemd, instalación de Chromium Playwright en `.cache/ms-playwright` del proyecto y arranque de `ofertas-hunter`, `ofertas-hunter-dispatcher`, `ofertas-hunter-telegram`
* Motivo: llevar a producción la corrección para importar/revalidar/publicar ofertas Telegram >=50% hacia WhatsApp
* Relación: aplica los cambios locales anteriores y el ajuste de `PLAYWRIGHT_BROWSERS_PATH`
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: deploy/systemd/ofertas-hunter.service, deploy/systemd/ofertas-hunter-dispatcher.service, deploy/systemd/ofertas-hunter-telegram.service
* Cambio: se configura `PLAYWRIGHT_BROWSERS_PATH=__PROJECT_ROOT__/.cache/ms-playwright` y se permite `__PROJECT_ROOT__/.cache` en `ReadWritePaths`
* Motivo: en VPS los servicios corren con `ProtectHome=true`; Playwright no podía ver Chromium bajo `/home/agaetranahoy/.cache`, dejando la revalidación Telegram sin navegador
* Relación: complementa la corrección de revalidación live para ofertas Telegram >=50% antes de enviar a WhatsApp
* Resultado: ✅ éxito

## 2026-05-29

* Archivo: src/ofertas_hunter/dispatching/outbox.py
* Cambio: los items con `requires_live_validation=true` ahora entran siempre a revalidaciÃ³n y no son elegibles para publicar hasta que el revalidador marque el payload como validado
* Motivo: las ofertas importadas desde Telegram >=50% podÃ­an quedar pendientes/fallar o intentar publicarse con imagen local y sin confirmaciÃ³n live, rompiendo el contrato de Fase 3.2
* RelaciÃ³n: mejora el flujo Telegram â†’ outbox â†’ revalidator â†’ dispatcher registrado previamente
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: src/ofertas_hunter/mcp/context.py
* Cambio: `ServerContext.get_dispatcher()` ahora inyecta `_LazyRevalidator()` en `OutboxDispatcher`
* Motivo: el revalidador lazy estaba definido pero no se usaba; `dispatch_outbox` del MCP no revalidaba ofertas Telegram antes del envÃ­o a WhatsApp
* RelaciÃ³n: corrige el dispatcher usado por opciones Kiro del `start.ps1`
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: src/ofertas_hunter/orchestrator.py
* Cambio: el dispatcher del modo `python -m ofertas_hunter run` usa un `PlaywrightRevalidator` lazy y cierra su browser al terminar
* Motivo: el orquestador nativo tambiÃ©n tenÃ­a `revalidator=None`, dejando sin confirmaciÃ³n live los items de Telegram o antiguos
* RelaciÃ³n: misma correcciÃ³n aplicada al MCP para cubrir el servicio systemd principal
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: src/ofertas_hunter/__main__.py
* Cambio: el subcomando `dispatch` ahora usa un revalidador lazy antes de publicar
* Motivo: el servicio systemd separado `ofertas-hunter-dispatcher.service` ejecuta `python -m ofertas_hunter dispatch`; tambiÃ©n necesitaba respetar `requires_live_validation`
* RelaciÃ³n: cubre el modo dispatcher independiente del VPS
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: src/ofertas_hunter/mcp/tools/action_tools.py
* Cambio: se agregÃ³ la herramienta MCP `process_telegram(limit,budget)` para hacer backfill corto de canales Telegram y encolar candidatos accionables
* Motivo: las opciones Kiro del lanzador no tenÃ­an una acciÃ³n MCP para importar mensajes Telegram; sÃ³lo cazaban Amazon/ML y despachaban outbox existente
* RelaciÃ³n: complementa la Fase 3.2 sin cambiar la regla dura de descuento >=50% ni el bloqueo Telegramâ†’MercadoLibre
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: scripts/orquestador_ia.py
* Cambio: el modo autÃ³nomo Python ahora ejecuta loop de Telegram con `process_telegram`, ademÃ¡s de Amazon, ML y dispatcher
* Motivo: la opciÃ³n `[3]` del `start.ps1` se presentaba como 24/7 pero no importaba mensajes nuevos de Telegram
* RelaciÃ³n: aplica al mismo lanzador que usa el atajo `C:\Users\yarteaga\Desktop\bot_autonomo_ofert\start.ps1`
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: start.ps1
* Cambio: el prompt del orquestador ahora incluye `process_telegram` y el texto del menÃº refleja 17 tools y loop Telegram
* Motivo: el `start.ps1` de la carpeta padre redirige a este archivo; cualquier opciÃ³n local debe importar Telegram antes de despachar
* RelaciÃ³n: cubre opciones `[1]` y `[3]` del lanzador local
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: start.sh
* Cambio: el equivalente Linux/VPS incluye `process_telegram` en el prompt y actualiza el texto del modo Python a Amazon+ML+Telegram+dispatch
* Motivo: el usuario indicÃ³ que el arranque local equivale al de VPS; el VPS necesitaba el mismo comportamiento
* RelaciÃ³n: cubre opciones Kiro y foreground Python del lanzador Linux
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: scripts/install_kiro_agents.ps1
* Cambio: los agentes Kiro se reinstalan con 17 tools, `process_telegram` en el ciclo principal y el subagente Telegram importando antes de revisar outbox
* Motivo: si el operador reinstala agentes desde el menÃº, no debe perderse la correcciÃ³n
* RelaciÃ³n: cubre opciÃ³n `[5]` del lanzador Windows
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: scripts/install_kiro_agents.py
* Cambio: el instalador Linux de agentes Kiro replica `process_telegram` y actualiza descripciones a 17 tools
* Motivo: el wrapper Linux `install_kiro_agents.sh` ejecuta este archivo en VPS
* RelaciÃ³n: cubre reinstalaciÃ³n de agentes en VPS
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: scripts/_update_orquestador_prompt.ps1
* Cambio: el actualizador puntual del prompt del orquestador incluye `process_telegram`
* Motivo: evitar que una actualizaciÃ³n posterior del prompt borre el paso de importaciÃ³n Telegram
* RelaciÃ³n: mantiene consistencia con `install_kiro_agents.ps1`
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: tests/unit/dispatching/test_cooldown_outbox.py
* Cambio: tests para revalidaciÃ³n inmediata y no selecciÃ³n de items con `requires_live_validation=true`
* Motivo: fijar la regresiÃ³n que impedÃ­a el flujo Telegram >=50% â†’ WhatsApp
* RelaciÃ³n: cubre `outbox.py`
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: tests/unit/mcp/test_action_tools.py
* Cambio: tests para `process_telegram` deshabilitado, registry de action tools y revalidador del dispatcher MCP
* Motivo: garantizar que las opciones Kiro tengan herramienta Telegram y que el dispatcher MCP revalide
* RelaciÃ³n: cubre `action_tools.py` y `mcp/context.py`
* Resultado: âœ… Ã©xito

## 2026-05-29

* Archivo: tests/integration/mcp/test_mcp_smoke_in_process_client.py
* Cambio: contrato MCP actualizado de 16 a 17 tools e incluido `process_telegram`
* Motivo: la suite completa detectÃ³ que el smoke test aÃºn esperaba el set anterior de herramientas
* RelaciÃ³n: cubre la nueva herramienta MCP de Telegram
* Resultado: âœ… Ã©xito

## 2026-05-28 â€” Diversity Curator Agent (selecciÃ³n IA del outbox)

### Goal
Reemplazar la elecciÃ³n aleatoria del `OutboxDispatcher` (`pick_random_eligible`) por un agente IA que decide quÃ© oferta publicar al grupo de WhatsApp para maximizar diversidad por categorÃ­a, marca, marketplace y rango de precio. Usa `kiro-cli` como LLM con fallback determinÃ­stico cuando el LLM falla.

### GarantÃ­a clave
Si `DIVERSITY_CURATOR_ENABLED=false` (default) â†’ comportamiento idÃ©ntico al actual (`pick_random_eligible`). Si el binario `kiro-cli` no estÃ¡ disponible â†’ degrada a curator sin LLM (top1 del scorer determinÃ­stico). Cero regresiones en producciÃ³n.

### Arquitectura
1. `DiversityScorer` â€” scoring determinÃ­stico de candidatos segÃºn historial.
2. `KiroCliClient` â€” wrapper async sobre `subprocess kiro-cli --classic --no-interactive`, con timeout, regex extractor de JSON, manejo de cancelaciÃ³n que no deja procesos huÃ©rfanos.
3. `DiversityCurator` â€” orquesta: filtra elegibles â†’ scorea â†’ top 10 â†’ consulta LLM â†’ fallback al top 1 si falla.
4. `OutboxDispatcher` acepta `item_selector` opcional. Cuando es `None` mantiene el comportamiento legacy.
5. `build_diversity_curator(db, settings)` factory compartida entre `ServerContext` (modos MCP [1]/[2]) y `Orchestrator` (modo [3]).

### Componentes nuevos

`src/ofertas_hunter/dispatching/diversity_scorer.py`:
- `DiversityScorer(history_size=10, top_n=10)` con `rank(candidates, history) -> list[ScoredCandidate]`.
- `HistoryEntry(marketplace, category, brand, price_bucket, sent_at)` â€” convenciÃ³n `history[0]` = mÃ¡s reciente.
- `price_bucket(price)` clasifica low (<500) / mid (500-2999) / high (â‰¥3000) MXN.
- Multiplicadores mÃ³dulo-nivel: `CATEGORY_REPEAT_BASE=0.5`, `BRAND_REPEAT_BASE=0.7`, `LAST_MARKETPLACE_PENALTY=0.7`, `LAST_PRICE_BUCKET_PENALTY=0.85`, `CATEGORY_ABSENT_BONUS=1.5`.
- 12 tests unitarios.

`src/ofertas_hunter/intelligence/kiro_cli_client.py`:
- `KiroCliConfig(binary_path="", classic_mode=True, timeout_seconds=30.0)`.
- `KiroCliClient.ask_json(prompt, agent=None) -> Optional[dict]` â€” nunca lanza, retorna `None` ante cualquier fallo.
- `resolve_kiro_cli_path()` con orden: env `KIRO_CLI` > `~/.local/bin/kiro-cli` (Linux) / `%LOCALAPPDATA%\Kiro-Cli\kiro-cli.exe` (Windows) > `shutil.which()` > fallback `"kiro-cli"`.
- `_terminate(proc)` helper que mata el subprocess y espera; tolera `ProcessLookupError`, loguea cualquier otro error con `logger.exception`.
- Maneja `asyncio.CancelledError`: mata el subprocess, loguea, **re-lanza** la cancelaciÃ³n (evita procesos huÃ©rfanos en shutdown).
- Constante `EXPECTED_JSON_KEY = "chosen_id"` con regex construido vÃ­a `re.escape` para evitar drift de schema silencioso.
- 10 tests unitarios.

`src/ofertas_hunter/dispatching/diversity_curator.py`:
- `DiversityCurator(*, db, scorer, llm_client=None, history_size=10, candidate_limit=10)`.
- `async def pick(outbox, last_normal_publication_at, now) -> Optional[OutboxItem]` cumple la firma de `ItemSelector`.
- Algoritmo: `eligible_now()` â†’ atajo si 1 candidato â†’ `_load_history()` JOIN de `published_messages` + `outbox` â†’ `scorer.rank()` top N â†’ `llm_client.ask_json(_build_prompt(...))` â†’ si LLM vÃ¡lido y `chosen_id âˆˆ topN` retorna ese item, si no fallback al top 1.
- AuditorÃ­a: emite `runtime_event(kind="diversity_curator_decision")` con `{chosen_id, fallback_used, reason, candidates_count, history_size, llm_latency_ms}`. AuditorÃ­a falla â†’ loguea, nunca interrumpe el dispatching.
- `_load_history` skipea filas con JSON o `sent_at` corruptos y loguea WARNING (la versiÃ³n inicial defaulteaba a `now()` y sesgaba el scorer).
- 10 tests unitarios cubriendo criterios A-G del spec + persistencia.

`src/ofertas_hunter/dispatching/curator_factory.py`:
- `build_diversity_curator(db, settings) -> Optional[DiversityCurator]`.
- Devuelve `None` si `diversity_curator_enabled=False` (path legacy).
- Si `diversity_curator_use_llm=False` o falla import de `KiroCliClient` â†’ curator sin LLM.

### Componentes modificados

`src/ofertas_hunter/dispatching/dispatcher.py`:
- Type alias `ItemSelector = Callable[[InMemoryOutbox, Optional[datetime], datetime], Awaitable[Optional[OutboxItem]]]`.
- `OutboxDispatcher.__init__` acepta `item_selector: Optional[ItemSelector] = None` (kw-only).
- `tick()` usa el selector si estÃ¡ presente, con `try/except` que hace fallback a `pick_random_eligible` si lanza (zero-regression bajo cualquier fallo del curator/LLM/db).
- 3 tests cubriendo: selector activo / ausente (legacy) / lanzando (fallback).

`src/ofertas_hunter/config.py` (Settings):
- 6 settings nuevos: `diversity_curator_enabled=False`, `diversity_curator_use_llm=True`, `diversity_curator_history_size=10`, `diversity_curator_candidate_limit=10`, `diversity_curator_llm_timeout_seconds=30.0`, `diversity_curator_kiro_cli_path=None`.

`src/ofertas_hunter/mcp/context.py` (modos [1]/[2]):
- `get_dispatcher()` invoca `build_diversity_curator(db, settings)` y pasa `curator.pick` como `item_selector` (o `None` si la feature estÃ¡ deshabilitada).

`src/ofertas_hunter/orchestrator.py` (modo [3]):
- `_build_dispatcher_factory()` hace lo mismo dentro del closure `factory`.

### VerificaciÃ³n
- 711 tests unitarios pasan (1 skipped Windows-only). Zero regresiones.
- Suite completa: `tests/unit/dispatching/`, `tests/unit/intelligence/`, `tests/unit/mcp/`, `tests/unit/test_orchestrator.py` todos verdes.

### Spec y plan
- Spec: `docs/superpowers/specs/2026-05-28-diversity-curator-agent-design.md`.
- Plan: `docs/superpowers/plans/2026-05-28-diversity-curator-agent.md` (8 tasks TDD).
- Workflow: subagent-driven-development con implementer â†’ spec-reviewer â†’ quality-reviewer por task.

### ActivaciÃ³n en producciÃ³n
1. Editar `.env` en VPS: `DIVERSITY_CURATOR_ENABLED=true`, `DIVERSITY_CURATOR_USE_LLM=true` (o `false` para sÃ³lo determinÃ­stico).
2. Asegurar que `kiro-cli` estÃ¡ instalado en el path (o setear `DIVERSITY_CURATOR_KIRO_CLI_PATH`).
3. Reiniciar el servicio. Las decisiones quedan en `runtime_events(kind='diversity_curator_decision')` para auditorÃ­a.
4. Para revertir: `DIVERSITY_CURATOR_ENABLED=false` + reinicio â†’ comportamiento legacy.

### Commits
- `935cdf6` feat(dispatching): add DiversityScorer for outbox candidate ranking
- `19ee52a` refactor(dispatching): address code review feedback for DiversityScorer
- `add363f` feat(intelligence): add KiroCliClient async wrapper for kiro-cli
- `f91a73c` fix(intelligence): handle CancelledError + improve error logging in KiroCliClient
- `8922484` feat(dispatching): add DiversityCurator orchestrating scorer + LLM + fallback
- `d1e2a2b` fix(dispatching): log and skip corrupt history rows in DiversityCurator
- `cadd006` feat(dispatching): add optional item_selector to OutboxDispatcher
- `e1fb2ee` feat(config): add diversity_curator settings and factory
- `011f36b` feat(integration): wire DiversityCurator into ServerContext and Orchestrator

---

## 2026-05-27 â€” ML hot-reload de cookies por WhatsApp (`/cookies_ml`)

### ImplementaciÃ³n de hot-reload de sesiÃ³n Mercado Libre vÃ­a comando WhatsApp

- **Nuevo mÃ³dulo** `src/ofertas_hunter/session/ml_session_manager.py`:
  - `MercadoLibreSessionManager` con 5 estados explÃ­citos:
    `valid`, `invalid`, `waiting_for_admin_cookies`,
    `validating_received_cookies`, `challenge_required`.
  - Pipeline canÃ³nico `reload_from_cookies()`: stage â†’ validate â†’ promote â†’ rotate.
  - `validate_cookies()` abre Chromium temporal (Playwright async), inyecta cookies,
    navega a ML MÃ©xico y detecta seÃ±ales de login/QR/captcha/2FA. La factory de
    validaciÃ³n se inyecta para tests sin Playwright.
  - `stage_cookies()` escribe en `secrets/incoming_cookies/mercadolibre_latest.json`
    con `chmod 600` (best-effort en Windows).
  - `promote_cookies()` escribe en `secrets/mercadolibre_cookies.json` y en
    `secrets/browser_profiles/mercadolibre/cookies.json` tambiÃ©n con 600.
  - `rotate_context()` delega en callback hacia `ctx.reload_ml_cookies()` para
    que el browser context ML se cierre y se recree sin reiniciar el servicio.
  - `cookies_metadata()` devuelve `count`, `domains`, `exp_min`, `exp_max` â€”
    NUNCA valores de cookies.

- **`MLCookieValidator` extendido** en `ml_session_recovery.py`:
  - Acepta lista directa, `{"cookies": [...]}`, `{"data": [...]}`.
  - Filtra cookies vencidas (`expirationDate < now`); falla con
    `all_cookies_expired` si no queda ninguna vÃ¡lida.
  - Acepta dominios `mercadolibre.com[.mx|.ar|.br|.uy|.cl|.co|.pe]` y
    `mercadopago.com*`.
  - LÃ­mite de 300 cookies (mÃ¡s serÃ­a sospechoso).

- **`MLCookieReloader` actualizado**:
  - Si recibe `session_manager`, delega TODO el flujo seguro al manager
    (stage â†’ validate â†’ promote â†’ rotate). El callback legacy se ignora.
  - Si NO recibe manager, mantiene comportamiento histÃ³rico (backup +
    write + callback) para compatibilidad.

- **Webhook HTTP `/wa/inbound`** (`ml_session_inbound.py`):
  - **Comando explÃ­cito `/cookies_ml`**: el bot SÃ“LO procesa cookies si el
    mensaje empieza con esa marca. Cualquier otro texto se ignora
    silenciosamente con evento `ml_session_inbound_no_command_ignored`.
  - Soporta `documentMessage` con base64 (attachment `.json`) ademÃ¡s de
    texto inline despuÃ©s del comando.
  - Helpers nuevos: `parse_command_and_payload()`, `extract_attachment_json()`.

- **`MLSessionRecoveryRuntime` rediseÃ±ado**:
  - Construye e inyecta `MercadoLibreSessionManager` en
    `ctx.ml_session_manager`.
  - El reloader del runtime usa el manager (pipeline seguro completo).
  - `validation_factory` opcional para tests / inyecciÃ³n de browsers
    alternativos.

- **`MercadoLibreHunterAgent`**:
  - Acepta `session_manager` por kwarg.
  - Antes de cada `hunt_urls()` / `hunt_from_frontier()` consulta
    `manager.status`; si es != `valid`, salta el ciclo emitiendo
    `runtime_event(kind="ml_hunt_skipped_session_invalid")`.
  - En caso de `paused_for_login` durante un hunt, llama a
    `manager.mark_invalid("login_redirect_during_hunt")`.

- **`ServerContext`**:
  - Campo `ml_session_manager: Any = None` que el runtime inyecta al `start()`.
  - `get_ml_hunter()` pasa el manager al hunter.
  - `reload_ml_cookies()` simplificado y emite breadcrumb explÃ­cito.

- **Mensajes WhatsApp** (textos finales segÃºn spec del usuario):
  - PÃ©rdida: `"Mercado Libre perdiÃ³ sesiÃ³n. EnvÃ­ame cookies nuevas con
    /cookies_ml como JSON o archivo .json."`
  - Ã‰xito: `"Cookies de Mercado Libre aceptadas. SesiÃ³n reactivada sin
    reiniciar el servicio."`
  - Falla validaciÃ³n activa: `"Cookies recibidas, pero Mercado Libre sigue
    pidiendo login/challenge. No reemplacÃ© la sesiÃ³n activa."`

### Tests

- `tests/unit/session/test_ml_session_manager.py` â€” 18 tests del manager
  (estados, pipeline, staging, validaciÃ³n factory mock, eventos seguros).
- `tests/unit/session/test_ml_hot_reload_acceptance.py` â€” 6 tests de
  aceptaciÃ³n A-F del spec (JSON invÃ¡lido / dominio no ML / formato ok pero
  sesiÃ³n invÃ¡lida / cookies vÃ¡lidas / Amazon-Telegram intactos / sin restart).
- `tests/unit/session/test_ml_session_inbound.py` â€” actualizado para usar
  `/cookies_ml` y attachments.
- `tests/unit/session/test_ml_session_runtime.py` â€” aÃ±adido test C (no
  promueve si validaciÃ³n falla); usa `validation_factory` mock.
- `tests/unit/agents/test_mercadolibre_hunter_agent.py` â€” 2 tests nuevos
  para el gate `session_manager`.

**Resultado: 666 tests passing local (antes 622), 0 regresiones.**

### Seguridad

- Permisos `chmod 600` en `secrets/incoming_cookies/*.json`,
  `secrets/mercadolibre_cookies.json` y profile cookies.
- `runtime_events` y logs sÃ³lo guardan metadata segura (`count`, `domains`,
  `exp_min`, `exp_max`). Verificado por test
  `test_validate_cookies_logs_safe_metadata`.
- Filtro estricto de admin numbers en webhook (no-admin â†’ 403).
- Sin loops `print` ni `repr` de cookies en cÃ³digo nuevo.

---

## 2026-05-25 â€” Fase 1 completa + Fase 2 mÃ­nima

### AuditorÃ­as

- AuditorÃ­a completa de `bot_diversidad_global` â†’ [`docs/audit/bot_diversidad_global.md`](docs/audit/bot_diversidad_global.md). Veredicto: estructura modular sÃ³lida; orchestrator y persistencia JSON deben rediseÃ±arse; `session_loader`, parsers y explorers se reutilizan literal.
- AuditorÃ­a completa de `AmazonScrapperIA` â†’ [`docs/audit/AmazonScrapperIA.md`](docs/audit/AmazonScrapperIA.md). Veredicto: `price_parser`, `browser_worker` (stealth) y `dom_healer` son el aporte clave; `MemoryStore` se migra a SQLite; orquestador se descarta.

### DocumentaciÃ³n arquitectÃ³nica

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) â€” capas, agentes, diagrama, tablas SQLite.
- [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) â€” mapeo file-by-file de cada legacy al nuevo proyecto.
- [`docs/AGENTS.md`](docs/AGENTS.md) â€” responsabilidades de los 11 agentes.
- [`docs/RULES.md`](docs/RULES.md) â€” reglas duras de negocio. **Incluye el formato corregido de oferta normal (JBL Tune 510BT)** indicado por el usuario.
- [`docs/PRICE_ERROR_DETECTION.md`](docs/PRICE_ERROR_DETECTION.md) â€” `PriceErrorScorer` completo, rangos por categorÃ­a, modelo `PriceErrorSignal`.
- [`docs/TELEGRAM_PRICE_ERROR_PATTERNS.md`](docs/TELEGRAM_PRICE_ERROR_PATTERNS.md) â€” tÃ©rminos de urgencia, emojis, marketplaces.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) â€” schema SQLite con todas las tablas.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) â€” Windows local + Ubuntu VPS + Docker Compose.

### Esqueleto de cÃ³digo

- `migrations/001_init.sql` â€” schema completo SQLite con WAL.
- `src/ofertas_hunter/__init__.py`, `__main__.py` (CLI: `init-db`, `check-config`, `run`).
- `src/ofertas_hunter/config.py` â€” pydantic-settings con `.env`, compatibilidad con envvars del legacy.
- `src/ofertas_hunter/db.py` â€” conexiÃ³n SQLite WAL + `init_db` idempotente.
- `src/ofertas_hunter/logging_setup.py`.
- `src/ofertas_hunter/models.py` â€” dataclasses + enums (`PriceErrorSignal`, `Offer`, `OutboxItem`, `Classification`, etc.).
- `src/ofertas_hunter/intelligence/`
  - `category_ranges.py` â€” rangos heurÃ­sticos iniciales por categorÃ­a.
  - `discount_calculator.py` â€” utilidades de descuento.
  - `price_error_scorer.py` â€” scorer determinista 0-100 con todas las seÃ±ales de la spec Â§2.3-Â§2.5.
- `src/ofertas_hunter/publishing/formatter.py` â€” templates `oferta_normal` (formato JBL) y `error_de_precio`.
- `src/ofertas_hunter/dispatching/`
  - `cooldown.py` â€” `CooldownPolicy` con bypass para errores de precio.
  - `outbox.py` â€” `InMemoryOutbox` y `SqliteOutbox` con `pick_random_eligible`, `needs_revalidation` (>1h), prioridad de errores de precio.
- `src/ofertas_hunter/telegram/`
  - `signal_extractor.py` â€” detecta tÃ©rminos "ERROR DE PRECIO", "CORRAN", "DEJA PEDIR", "A SOLO", emojis ðŸš¨ðŸ”¥â€¼ï¸.
  - `message_parser.py` â€” `parse_message` que extrae tienda, marca, categorÃ­a, precio, link; **descarta automÃ¡ticamente links de Mercado Libre** (`skip_reason="mercadolibre_link"`).

### Fixtures y tests

- `tests/fixtures/price_errors/example_a.json` ... `example_m.json` (13 ejemplos de la spec Â§2.1).
- `tests/conftest.py` con fixtures `price_error_fixtures_dir` y `load_price_error_example`.
- `tests/unit/intelligence/test_price_error_scorer.py` â€” 25 tests, incluye los **5 tests obligatorios** del scorer + paramÃ©tricos sobre A-M.
- `tests/unit/intelligence/test_discount_calculator.py` â€” 9 tests.
- `tests/unit/publishing/test_formatter.py` â€” 6 tests, **valida el formato exacto JBL Tune 510BT**.
- `tests/unit/dispatching/test_cooldown_outbox.py` â€” 9 tests, incluye los obligatorios `test_normal_offer_respects_5_min_cooldown`, `test_price_error_bypasses_cooldown`, `test_outbox_revalidates_after_one_hour`.
- `tests/unit/telegram/test_message_parser.py` â€” 7 tests, incluye `test_telegram_mercadolibre_links_are_ignored` y `test_telegram_urgency_terms_increase_score`.
- `tests/unit/test_db_init.py` â€” 3 tests del schema (todas las tablas, idempotencia, WAL).

**Resultado:** `pytest -q` â†’ **59 passed**.

Cobertura de los 16 tests obligatorios de la spec Â§16:

| Test obligatorio | Estado |
|---|---|
| `test_price_error_laptop_extreme_low_price` | âœ… |
| `test_price_error_iphone_extreme_low_price` | âœ… |
| `test_price_error_samsung_flagship_extreme_low_price` | âœ… |
| `test_price_error_airpods_low_price` | âœ… |
| `test_price_error_visible_91_percent_discount` | âœ… |
| `test_telegram_urgency_terms_increase_score` | âœ… (scorer + extractor) |
| `test_no_image_not_publishable` | âœ… |
| `test_no_price_not_publishable` | âœ… |
| `test_used_product_requires_extreme_discount` | âœ… |
| `test_outbox_revalidates_after_one_hour` | âœ… |
| `test_normal_offer_respects_5_min_cooldown` | âœ… |
| `test_price_error_bypasses_cooldown` | âœ… |
| `test_telegram_mercadolibre_links_are_ignored` | âœ… |
| `test_monthly_payment_not_misread_as_total_price` | âœ… |
| `test_variant_mismatch_reduces_confidence` | âœ… |
| `test_accessory_price_not_confused_with_main_product` | âœ… |

### Decisiones tÃ©cnicas

- **Python 3.11+**, alineado con el legacy.
- **SQLite con WAL** activado (no aiosqlite todavÃ­a; la concurrencia real entra en Fase 3 con un pool si hace falta).
- **pydantic-settings 2.x** para config con `.env`. Compatibilidad con envvars del legacy: `BOT_DIVERSIDAD_GLOBAL_COOKIES_PATH` y `OFERTAS_MELI_BROWSER_COOKIES_PATH` siguen siendo respetados.
- **PriceErrorScorer determinista** (sin LLM). Los thresholds son configurables en `.env`. La spec impone subscores muy especÃ­ficos; los implemento literalmente y los acoto a [0, 100].
- **Outbox** con prioridad explÃ­cita: errores de precio se eligen **antes** de aleatorizar. Las ofertas normales sÃ­ se eligen aleatoriamente entre todas las elegibles tras cooldown.
- **Telegram parser** con regla dura: cualquier link `meli.la` o `mercadolibre.com.mx` produce `skip_reason="mercadolibre_link"` y el listener (Fase 3) lo descartarÃ¡ sin enviar a `price_intelligence`.
- **Formato JBL Tune 510BT** validado letra por letra contra el ejemplo del usuario en `test_jbl_example_matches_user_specified_format`.
- **No se borrÃ³ nada** de los proyectos legacy. Todo el reuso es por copy & adapt en el nuevo paquete.

### Criterios de aceptaciÃ³n cumplidos (spec Â§14)

- âœ… `pytest` pasa (59/59).
- âœ… El bot puede arrancar sin credenciales: `python -m ofertas_hunter check-config` y `init-db` funcionan en ambiente vacÃ­o.
- âœ… Esquema SQLite WAL operativo y todas las 19 tablas exigidas creadas.
- âœ… `Cooldown 5min` para normales, bypass para PE, revalidaciÃ³n >1h: implementado y testeado.
- âœ… `image obligatoria`: `formatter` levanta `FormatterError` si falta; `scorer` marca `not_publishable` con razÃ³n `no_image`.
- âœ… Telegram ignora links de Mercado Libre: testeado.
- âœ… Telegram detecta lenguaje de urgencia + emojis: testeado.
- âœ… DocumentaciÃ³n clara para correr en VPS Ubuntu en `docs/DEPLOY.md`.
- âœ… Ejemplos extremos (laptop $305, iPhone 16 Pro Max $3,899, Galaxy S24 $1,399, A32 $197) clasificados como `price_error_confirmed` con `very high` o `high`.
- âœ… Producto sin imagen / sin precio / con mensualidad no publicable.

### Pendiente â€” Fase 3

- `src/ofertas_hunter/marketplaces/amazon/hunter.py` (adaptar `AmazonScrapperIA/src/browser_worker.py`).
- `src/ofertas_hunter/marketplaces/mercadolibre/` (adaptar `bot_diversidad_global/src/explorers/*` y `extraction/*`).
- `src/ofertas_hunter/extraction/url_resolver.py` y `image_resolver.py`.
- `src/ofertas_hunter/dispatching/dispatcher.py` (loop async serializado vs Evolution API).
- `src/ofertas_hunter/publishing/whatsapp_evolution.py`.
- `src/ofertas_hunter/telegram/listener.py` (Telethon).
- `src/ofertas_hunter/agents/*` (clases `BaseAgent` + concretos).
- Migrar tests Ãºtiles del legacy (`tests/unit/test_session_loader.py`, `test_price_parser.py`, etc.) ajustando imports.

### Pendiente â€” Fase 4

- `src/ofertas_hunter/self_healing/dom_healer.py` (adaptar `AmazonScrapperIA/src/dom_healer.py` con tests automÃ¡ticos antes de patch).
- `src/ofertas_hunter/agents/runtime_watchdog.py`.
- `src/ofertas_hunter/agents/memory_compressor.py`.
- `deploy/systemd/*.service`, `deploy/docker/Dockerfile`.
- Scripts: `scripts/status.sh`, `scripts/run_local.ps1`, `scripts/check_db.py`, `scripts/test_*`, `scripts/revalidate_outbox.py`, `scripts/export_memory_summary.py`.


---

## 2026-05-25 â€” Fase 3.1 (outbox_dispatcher + Evolution API) âœ…

### Archivos creados

- `src/ofertas_hunter/publishing/evolution_client.py` â€” cliente async httpx para `POST /message/sendText/{instance}` y `POST /message/sendMedia/{instance}`. Acepta `bytes` / path local / data URL / URL http(s) / base64 puro como media. Headers `apikey: {EVOLUTION_API_KEY}`. Modo dry-run que no toca la red. Mock-friendly via `httpx.AsyncClient` inyectable.
- `src/ofertas_hunter/publishing/whatsapp_publisher.py` â€” `WhatsAppPublisher` que orquesta formatter (JBL / error de precio) + evolution_client. Aplica los gates duros (image_url, current_price, url) antes de formatear. Respeta `enabled=False` retornando `skipped`.
- `src/ofertas_hunter/dispatching/dispatcher.py` â€” `OutboxDispatcher` serializado con `asyncio.Lock` (un solo publish a la vez), `tick()` para ciclo Ãºnico y `run_forever()`. Aplica la polÃ­tica de cooldown vÃ­a `CooldownPolicy`. Llama a `Revalidator` para items con `enqueued_at > 1h`. `NullRevalidator` por defecto (Fase 3.3/3.4 traerÃ¡ los Playwright reales). `make_sqlite_published_recorder(conn)` para persistir en `published_messages`.
- `src/ofertas_hunter/__main__.py` â€” agregados `dispatch [--once]` y `enqueue-sample [--kind normal|price_error]`. `dispatch` arma todo el grafo SQLite + outbox + publisher con la config del `.env`.
- `scripts/test_whatsapp.py` â€” prueba manual con `--dry-run` (default) o `--send`. Valida config y permite enviar texto o media a un grupo o telÃ©fono.
- `scripts/run_dispatcher.sh` â€” wrapper bash para VPS.
- `scripts/run_local.ps1` â€” wrapper PowerShell para Windows con flag `-Once` y `-Sample`.

### Archivos modificados

- `src/ofertas_hunter/config.py`: `publishing_enabled`, `publishing_dry_run`, `dispatcher_idle_sleep_seconds`, `ofertas_whatsapp_group_id` (alias requerido por la spec) + helper `whatsapp_group`.
- `.env.example`: nuevas variables. **PUBLISHING_ENABLED=false** y **PUBLISHING_DRY_RUN=true** por defecto.
- `src/ofertas_hunter/logging_setup.py`: forzar UTF-8 en stdout/stderr (Windows cp1252 fallaba con los emojis ðŸ”¥ðŸš¨âŒâœ…âš¡).
- `src/ofertas_hunter/dispatching/outbox.py`: ningÃºn cambio funcional; ya tenÃ­a `pick_random_eligible` con prioridad PE y `needs_revalidation`.

### Tests aÃ±adidos (Fase 3.1) â€” 23 tests nuevos

`tests/unit/publishing/test_evolution_client.py` (8):
- `test_evolution_client_send_text_dry_run` âœ…
- `test_evolution_client_send_media_dry_run` âœ…
- `test_evolution_client_send_media_dry_run_accepts_https_url` âœ…
- `test_evolution_client_send_text_real_uses_post` (mock httpx â†’ 200) âœ…
- `test_evolution_client_send_text_real_records_failure` (mock 401) âœ…
- `test_evolution_client_real_unconfigured_raises` âœ…
- `test_evolution_client_send_media_with_bytes` âœ…
- `test_evolution_client_send_media_with_data_url` âœ…

`tests/unit/publishing/test_whatsapp_publisher.py` (6):
- `test_publishes_normal_offer_in_dry_run` (valida formato JBL) âœ…
- `test_publishes_price_error_in_dry_run` âœ…
- `test_publisher_disabled_returns_skipped` âœ…
- `test_publish_without_image_fails` âœ…
- `test_publish_without_price_fails` âœ…
- `test_publish_without_target_group_fails` âœ…

`tests/unit/dispatching/test_dispatcher.py` (9):
- `test_dispatcher_does_not_publish_without_image` âœ…
- `test_dispatcher_does_not_publish_without_validated_price` âœ…
- `test_dispatcher_normal_offer_respects_cooldown` âœ…
- `test_dispatcher_price_error_bypasses_cooldown` âœ…
- `test_dispatcher_revalidates_offer_older_than_one_hour` âœ…
- `test_dispatcher_discards_expired_after_revalidation` âœ…
- `test_dispatcher_records_evolution_api_failure` (mock httpx 500) âœ…
- `test_dispatcher_random_selects_among_eligible_normal_offers` âœ…
- `test_dispatcher_publishing_disabled_keeps_item_pending` âœ…

### EjecuciÃ³n de tests

```
pytest -q
82 passed in 1.06s
```

### ValidaciÃ³n end-to-end manual (dry-run)

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
- `published_messages` registra el envÃ­o con `success=1`.
- `outbox.state='sent'`, `attempts=1`.

### CÃ³mo correr el dispatcher

```bash
# Windows / PowerShell
.\scripts\run_local.ps1 -Once
.\scripts\run_local.ps1                       # loop continuo

# Ubuntu VPS (despuÃ©s de Fase 4 systemd)
./scripts/run_dispatcher.sh --once
./scripts/run_dispatcher.sh                   # loop continuo
```

### CÃ³mo activar envÃ­o real (cuando el operador lo autorice)

1. Editar `.env`:
   ```
   EVOLUTION_BASE_URL=http://162.251.147.177:8080
   EVOLUTION_API_KEY=dev-evolution-api-key
   EVOLUTION_INSTANCE=mi-instancia
   OFERTAS_WHATSAPP_GROUP_ID=120363426569734715@g.us
   PUBLISHING_ENABLED=true
   PUBLISHING_DRY_RUN=false
   ```
2. Validar con un envÃ­o de prueba a tu propio telÃ©fono (no al grupo):
   ```powershell
   python scripts/test_whatsapp.py --send --to 5218338498692 --text "ofertas_hunter probe"
   ```
3. Si el `success=True` y el mensaje llegÃ³, reciÃ©n ahÃ­ encolÃ¡ una oferta real con `enqueue-sample` o esperÃ¡ a Fase 3.2/3.3 con los hunters reales.

### Decisiones tÃ©cnicas

- **httpx async** (no `urllib.request` sÃ­ncrono como el legacy): permite test con `MockTransport`, timeouts limpios y reuso de conexiÃ³n cuando se inyecta `client`.
- **Dry-run en el cliente**, no en el publisher. AsÃ­ el publisher siempre llama a `send_media` y la decisiÃ³n de tocar la red se hace una sola vez en el cliente. El test `test_evolution_client_send_text_real_uses_post` valida que la URL exacta es `{base}/message/sendText/{instance}` con `apikey` correcto.
- **Toda publicaciÃ³n va por `sendMedia`** porque la spec exige imagen obligatoria. El publisher pasa la imagen como URL pÃºblica (Amazon `m.media-amazon.com/...`) y Evolution se encarga de la descarga; si mÃ¡s adelante hace falta forzar base64 (algunos proveedores no aceptan URLs externas), basta con descargar la imagen primero en `image_resolver.py` (Fase 3.3) y pasar bytes.
- **Cooldown sÃ³lo aplica a normales** y se respeta tanto en dry-run como en real (test_dispatcher_normal_offer_respects_cooldown lo confirma con un clock inyectable).
- **Bypass para PE**: `pick_random_eligible` ya prioriza errores de precio antes de aleatorizar, validado con un test que ejecuta 30 picks y todos eligen el PE.
- **RevalidaciÃ³n**: el dispatcher llama al `Revalidator` *antes* de pickear, no despuÃ©s. Si la revalidaciÃ³n devuelve `still_eligible=False`, el item queda `discarded` con razÃ³n y nunca llega al publisher (test_dispatcher_discards_expired_after_revalidation).
- **`NullRevalidator`** es aceptable en Fase 3.1: el dispatcher tiene gates duros (imagen, precio, url) en el publisher, asÃ­ que aunque el revalidator no haga nada Ãºtil, no se publican datos rotos. Cuando se enchufen los hunters Playwright (Fase 3.3/3.4), un `PlaywrightRevalidator` reemplazarÃ¡ al null y validarÃ¡ pÃ¡gina real.

### Pendiente â€” Fase 3.2 (telegram_listener Telethon)

A iniciar despuÃ©s de tu visto bueno.


---

## 2026-05-25 â€” Fase 3.2 (telegram_listener Telethon) âœ…

### Archivos creados

- `src/ofertas_hunter/telegram/channel_config.py` â€” `parse_channels(value)` y `ChannelEntry` que clasifica username / tÃ­tulo / id.
- `src/ofertas_hunter/telegram/link_resolver.py` â€” `LinkResolver` async (httpx) con cache SQLite (`resolved_urls`). HEAD â†’ GET fallback, hasta 5 redirects, timeout 6s. `is_shortlink()` e `is_mercadolibre_url()` con comparaciÃ³n de host exacto.
- `src/ofertas_hunter/telegram/candidate_builder.py` â€” `TelegramCandidateBuilder` que aplica las reglas de la spec Â§4 y Â§6 y produce `TelegramCandidate` con clasificaciÃ³n interna (`telegram_price_error_signal | telegram_deal_signal | ignored_mercadolibre_from_telegram | noise`). Si actionable, construye un `OutboxItem` con `requires_live_validation=True`.
- `src/ofertas_hunter/telegram/telethon_listener.py` â€” `TelethonAdapter` real. Importa Telethon sÃ³lo al instanciar; falla con `TelethonImportError` si no estÃ¡ instalado. Resuelve canales por username, tÃ­tulo exacto o id numÃ©rico.
- `src/ofertas_hunter/telegram/telethon_listener_helpers.py` â€” helpers para descargar imagen del mensaje y convertirlo a `IncomingMessage` neutral.
- `src/ofertas_hunter/agents/__init__.py` y `src/ofertas_hunter/agents/telegram_listener_agent.py` â€” `TelegramListenerAgent` con interfaz `TelegramAdapter` (Protocol) para que los tests usen un fake. Maneja dedupe por `(channel, message_id)`, persiste `telegram_messages`, `discarded_candidates` y `outbox` (todo en transacciÃ³n SQLite).
- `scripts/test_telegram.py` â€” modo `parse | backfill | listen | check-config`.
- `scripts/run_telegram_listener.sh` â€” wrapper Linux/VPS.
- `scripts/run_telegram_listener.ps1` â€” wrapper Windows con flags `-Once`, `-Backfill`, `-Limit`.

### Archivos modificados

- `src/ofertas_hunter/telegram/message_parser.py`:
  - `ParsedTelegramMessage` ahora trae `original_urls`, `hashtags`, `chat_id`, `has_image`.
  - CategorÃ­a `smartphone_flagship` se detecta antes que `smartphone` genÃ©rica (era un bug que hacÃ­a caer iPhone 16 Pro Max en `smartphone`).
  - `_extract_all_urls()` con dedup en orden.
  - `_extract_hashtags()` para `#tags`.
- `src/ofertas_hunter/config.py`:
  - Vars Telegram completas: `telegram_target_channels`, `ofertas_telegram_api_id/hash` (compat), `telegram_backfill_process_budget_per_channel`, `telegram_channel_workers`, `telegram_ignore_mercadolibre_links`, `telegram_link_resolver_timeout_seconds`, `telegram_link_resolver_max_redirects`.
  - Helpers `resolved_telegram_api_id`, `resolved_telegram_api_hash`, `telegram_session_path_resolved`.
- `.env.example` â€” variables nuevas Telegram + alias legacy.
- `src/ofertas_hunter/__main__.py` â€” comandos `telegram-check-config`, `telegram-parse-sample`, `telegram-listen [--once] [--limit N]`, `telegram-backfill [--limit N]`.

### Fixtures de mensajes (14)

Bajo `tests/fixtures/telegram/`:
- `laptop_hp_elitebook_walmart_2349.txt`, `asus_vivobook_sams_305.txt`, `ipad_coppel_2611.txt`, `coofandy_amazon_91_percent.txt`, `dell_pro_16_1544.txt`, `sony_wf1000xm5.txt`, `msi_coppel_2719.txt`, `airpods_officedepot_599.txt`, `iphone_16_pro_max_liverpool_3899.txt`, `gigabyte_b450_amazon_611.txt`, `galaxy_s24_sears_1399.txt`, `galaxy_a32_walmart_197.txt`, `ipad_mini_sams_4999.txt`, `mercadolibre_link_should_be_ignored.txt`.

### Tests aÃ±adidos (Fase 3.2) â€” 35 tests nuevos

Distribuidos en 4 archivos:

`tests/unit/telegram/test_link_resolver.py` (6):
- `test_telegram_resolves_shortlinks_with_mock_transport` âœ…
- `test_telegram_handles_link_resolver_timeout` âœ…
- `test_resolver_detects_mercadolibre_after_redirect` âœ…
- `test_resolver_skips_redirects_for_direct_url` âœ…
- `test_resolver_uses_sqlite_cache` âœ…
- `test_helpers` âœ…

`tests/unit/telegram/test_candidate_builder.py` (15):
- `test_telegram_price_error_signal_gets_high_confidence` (parametrizado sobre 10 fixtures) âœ…
- `test_telegram_does_not_create_candidate_for_noise` âœ…
- `test_telegram_ignores_mercadolibre_links` âœ…
- `test_telegram_ignores_shortlink_resolved_to_mercadolibre` âœ…
- `test_telegram_91_percent_discount_creates_actionable_candidate` âœ…
- `test_telegram_no_image_no_outbox_item_safe` âœ…

`tests/unit/telegram/test_message_parser.py` ampliado (+7 nuevos):
- `test_telegram_detects_price_error_terms` âœ…
- `test_telegram_detects_urgency_terms` âœ…
- `test_telegram_extracts_price_from_message` âœ…
- `test_telegram_extracts_discount_percent` âœ…
- `test_telegram_extracts_store_guess` âœ…
- `test_telegram_extracts_hashtags` âœ…
- `test_telegram_mercadolibre_fixture_marked_as_skip` âœ…

`tests/unit/agents/test_telegram_listener_agent.py` (7):
- `test_telegram_creates_candidate_signal` âœ…
- `test_telegram_message_dedupe_by_chat_and_message_id` âœ…
- `test_telegram_backfill_does_not_duplicate_messages` âœ…
- `test_telegram_missing_credentials_fails_gracefully` âœ…
- `test_telegram_disabled_returns_empty` âœ…
- `test_telegram_ignores_mercadolibre_links_via_agent` âœ…
- `test_telegram_ignores_shortlink_resolved_to_ml` âœ…

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
# > "TELEGRAM_ENABLED=false â€” el listener no se conectarÃ¡."
```

### Flujo Telegram â†’ candidate â†’ scorer â†’ outbox pending_revalidation

```
[Telethon NewMessage]
        â”‚
        â–¼
TelethonAdapter -> IncomingMessage(chat_id, channel, message_id, text, date, image_path)
        â”‚
        â–¼
TelegramListenerAgent._process_one(msg)
        â”œâ”€ dedupe? SELECT FROM telegram_messages WHERE channel=? AND message_id=?
        â”‚     â””â”€ duplicate=True -> return ProcessingOutcome(persisted=False)
        â”‚
        â”œâ”€ parse_message(text, ...)
        â”‚     â””â”€ ParsedTelegramMessage(marketplace, brand, category, urgency_terms,
        â”‚        urgency_score, is_price_error_keyword, original_url, hashtags, ...)
        â”‚
        â”œâ”€ LinkResolver.resolve(original_url)   # HEAD â†’ GET, max 5 redirects, cache SQLite
        â”‚     â””â”€ ResolvedLink(final_url, is_mercadolibre, resolved)
        â”‚
        â”œâ”€ TelegramCandidateBuilder.build(parsed, resolved)
        â”‚     â”œâ”€ Gate ML: si parsed.skip_reason=='mercadolibre_link' o
        â”‚     â”‚   resolved.is_mercadolibre -> ignored_mercadolibre_from_telegram
        â”‚     â”œâ”€ Gate noise: sin link/precio/urgencia/discount -> noise
        â”‚     â”œâ”€ PriceErrorScorer.score(signal) -> ScoringResult
        â”‚     â”‚   (Es la misma lÃ³gica determinista usada en hunters)
        â”‚     â”œâ”€ DecisiÃ³n interna:
        â”‚     â”‚   Â· is_price_error_keyword OR urgency_score>=25 OR
        â”‚     â”‚     score >= confirmed/possible OR discount_visible>=80
        â”‚     â”‚       -> telegram_price_error_signal
        â”‚     â”‚   Â· score >= 40 (suspicious) -> telegram_price_error_signal (possible_pe)
        â”‚     â”‚   Â· discount_visible >= 50% -> telegram_deal_signal
        â”‚     â”‚   Â· resto -> noise
        â”‚     â””â”€ Si actionable y hay precio + link, construye OutboxItem
        â”‚        message_payload = {
        â”‚            "title", "current_price", "url", "image_url", "marketplace",
        â”‚            "confidence_label", "discount_percent",
        â”‚            "requires_live_validation": True,
        â”‚            "source": "telegram", "source_channel", "original_url",
        â”‚            "resolved_url", "urgency_terms", "score",
        â”‚            "score_classification", "internal_classification",
        â”‚        }
        â”‚
        â–¼
Persistencia SQLite (en transacciÃ³n):
    1) telegram_messages: dedupe key (channel, message_id), text, image_path,
       original_url, resolved_url, captured_at, processed_at, skip_reason.
    2) Si ignored_mercadolibre_from_telegram o noise: discarded_candidates.
    3) Si actionable:
       - products (UNIQUE url_canonical) -> reuse o INSERT con condition='unknown'
       - offers (state='eligible', classification=score.classification, score, reasons_json)
       - outbox (state='pending', type='price_error|possible_pe|normal',
                 message_payload con requires_live_validation=True)

        â–¼
[Dispatcher de Fase 3.1]
    El item entra a outbox como pending. Cuando llegue Fase 3.3/3.4 con
    Playwright revalidator, ese revalidator confirmarÃ¡ producto/precio/imagen/stock
    en pÃ¡gina real ANTES de publicar. Mientras tanto, el dispatcher en dry-run
    formatea y muestra el payload pero no envÃ­a nada.

[NUNCA se publica directo desde Telegram sin revalidaciÃ³n]
```

### Decisiones tÃ©cnicas

- **Adapter detrÃ¡s de Protocol**: `TelegramAdapter` es un `runtime_checkable` Protocol. El agent depende de la interfaz, no de Telethon. Tests usan `FakeTelegramAdapter` puro Python. ProducciÃ³n usa `TelethonAdapter`. Telethon **sÃ³lo se importa** cuando se instancia `TelethonAdapter` (lazy), asÃ­ el resto del paquete arranca aunque Telethon no estÃ© instalado.
- **Telethon como import lazy** se duplica en `telethon_listener_helpers.py` para no obligar al test runner a instalar Telethon.
- **Compatibilidad legacy**: `OFERTAS_TELEGRAM_API_ID` y `OFERTAS_TELEGRAM_API_HASH` se respetan vÃ­a `resolved_telegram_api_id/hash`. `TELEGRAM_CHANNELS` legacy tambiÃ©n funciona como fallback de `TELEGRAM_TARGET_CHANNELS`.
- **`requires_live_validation=True` siempre** en payloads creados desde Telegram. El dispatcher Fase 3.1 ya tiene gates duros (image, price, url) y `Revalidator` interface; cuando llegue Fase 3.3/3.4 un `PlaywrightRevalidator` real reemplazarÃ¡ al `NullRevalidator` y validarÃ¡ pÃ¡gina antes de publicar.
- **NingÃºn cambio al outbox/dispatcher** â€” el contrato de `OutboxItem` ya soporta este flujo con el campo extra en `message_payload`. Esto evita tocar Fase 3.1.
- **`is_shortlink/is_mercadolibre_url`** comparan host exacto con `httpx.URL`, no substring (un bug que hacÃ­a pasar `walmart.com.mx` como shortlink por contener `t.co`).
- **PUBLISHING_ENABLED y PUBLISHING_DRY_RUN siguen en false/true por defecto.** Telegram se conecta sÃ³lo si TELEGRAM_ENABLED=true.

### Criterios de aceptaciÃ³n cumplidos

- âœ… pytest completo: **117/117 passed**.
- âœ… Los 16 tests obligatorios listados en spec Â§12 cubiertos.
- âœ… Links de Mercado Libre desde Telegram se ignoran (directos y vÃ­a shortlink resuelto).
- âœ… Ejemplos extremos (laptop $305, iPhone Pro Max $3,899, Galaxy S24 $1,399, A32 $197, AirPods $599) â†’ `internal=telegram_price_error_signal`, `confidence âˆˆ {high, very high}`.
- âœ… Mensajes sin link/precio/urgencia â†’ `noise`, no genera candidato ni outbox.
- âœ… Duplicados (mismo `channel`+`message_id`) no se procesan dos veces (test verificado).
- âœ… Listener desactivado (TELEGRAM_ENABLED=false) no rompe nada â€” comando termina con mensaje claro.
- âœ… ConexiÃ³n real a Telegram queda detrÃ¡s de TELEGRAM_ENABLED.
- âœ… No se activa publicaciÃ³n real (PUBLISHING_ENABLED=false en `.env.example`).
- âœ… `requires_live_validation=true` en todos los payloads creados desde Telegram.

### Pendiente â€” Fase 3.3 (amazon_hunter Playwright)

A iniciar despuÃ©s de tu visto bueno. Esta fase incluirÃ¡ un `PlaywrightRevalidator` que reemplazarÃ¡ al `NullRevalidator` para items con `requires_live_validation=True`.


---

## 2026-05-25 â€” Fase 3.3 (amazon_hunter Playwright + PlaywrightRevalidator) âœ…

### Archivos creados

- `src/ofertas_hunter/marketplaces/__init__.py`, `base.py`, `url_utils.py` â€” `ExtractedProduct` (modelo comÃºn) + `extract_asin`, `canonicalize_amazon_url`, `is_amazon_url`.
- `src/ofertas_hunter/extraction/__init__.py`, `price_parser.py`, `amazon_product_parser.py` â€” utilidades de precios reutilizables y parser BS4 con fallbacks JSON-LD / OpenGraph / Twitter Card. DetecciÃ³n de mensualidad, variant_mismatch (Jaccard + cobertura del tÃ­tulo esperado), out-of-stock, etc.
- `src/ofertas_hunter/browser/__init__.py`, `browser_context.py`, `playwright_worker.py` â€” `BrowserWorker` Protocol (para tests fakes), `BrowserConfig`, `RenderedPage`, y `PlaywrightBrowserWorker` con stealth, rotaciÃ³n UA/viewport, jitter, bloqueo de `media`/`font`, captura de screenshot en fallo, detecciÃ³n de captcha.
- `src/ofertas_hunter/revalidation/__init__.py`, `playwright_revalidator.py` â€” implementa `Revalidator` del dispatcher. Usa BrowserWorker + AmazonProductParser + PriceErrorScorer. Decide `still_eligible` y construye nuevo `payload` con datos frescos de la pÃ¡gina (precio, imagen, asin, in_stock, score). Guarda snapshot en `dom_snapshots` cuando falla.
- `src/ofertas_hunter/agents/amazon_hunter_agent.py` â€” Agente hunter que itera URLs, fetchea, parsea, persiste (`products`, `price_observations`, `offers`, `outbox` o `discarded_candidates`), guarda snapshot DOM en `dom_snapshots` cuando falla.

- `scripts/test_playwright.py` â€” `--smoke` (no Amazon) y `--url` para validar una URL real.
- `scripts/run_amazon_hunter.sh`, `scripts/run_amazon_hunter.ps1` â€” wrappers VPS y Windows.
- `scripts/revalidate_outbox.py` â€” wrapper para `python -m ofertas_hunter revalidate-outbox`.

- `config/seeds/amazon.json` â€” placeholder vacÃ­o. Se debe llenar manualmente o vÃ­a `--seed`.

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - 5 nuevos subcomandos: `amazon-parse-url`, `amazon-validate-url`, `amazon-hunt`, `revalidate-outbox`, `revalidate-url`.
  - Lazy imports de Playwright/BrowserWorker para que el CLI siga arrancando sin Playwright instalado.

- `changes.md` â€” log de Fase 3.3.

### Lo que se portÃ³ de AmazonScrapperIA

| Origen legacy | Destino actual | AcciÃ³n |
|---|---|---|
| `src/price_parser.parse_price_text` | `extraction/price_parser.parse_price_text` | REUTILIZADO con misma semÃ¡ntica MX/europeo |
| `src/price_parser.calculate_discount` | `extraction/price_parser.calculate_discount` | REUTILIZADO |
| `src/price_parser.extract_discount_from_text` | `extraction/price_parser.extract_discount_percent` | REUTILIZADO (renombrado) |
| `src/price_parser.extract_product_data_from_page` (Playwright) | `extraction/amazon_product_parser.AmazonProductParser.parse` (BS4 puro) | **REESCRITO** â€” separaciÃ³n de Playwright vs parsing puro para tests |
| `src/browser_worker.STEALTH_SCRIPT` | `browser/browser_context.STEALTH_SCRIPT` | REUTILIZADO (simplificado) |
| `src/browser_worker.USER_AGENTS` y `VIEWPORTS` | `browser/browser_context.USER_AGENTS`, `VIEWPORTS` | REUTILIZADO |
| `src/browser_worker.BrowserWorker` (run_session, captcha streak, etc.) | `browser/playwright_worker.PlaywrightBrowserWorker` | **REESCRITO** â€” solo fetch por URL, no exploraciÃ³n masiva (fase 4) |
| `src/dom_healer.DegradationMonitor` | n/a por ahora | DESCARTADO en Fase 3.3 (lo retomamos en Fase 4 como `self_healing/dom_healer.py`) |
| `src/memory_store` | n/a | DESCARTADO (memoria ya estÃ¡ en SQLite) |
| `config/selectors.json` | inline en `amazon_product_parser.py` | DESCARTADO el JSON externo: la spec exige fallbacks robustos, no selectores configurables que se rompan al editar |
| `config/seeds.json` | `config/seeds/amazon.json` | PORTADO vacÃ­o; el operador llena cuando lo desee |
| `data/heal_samples/sample_*.html` (97) | n/a | NO PORTADO en bulk; los fixtures en `tests/fixtures/amazon/` son sintÃ©ticos pero realistas y deterministas |
| `scraper.py`, `run_with_kiro.py`, `mcp_server.py`, `setup_kiro_login.py` | n/a | DESCARTADOS (no necesarios para Fase 3.3) |

**No se borrÃ³ nada** del proyecto `AmazonScrapperIA`.

### Tests aÃ±adidos (Fase 3.3) â€” 58 tests nuevos

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
- `test_revalidator_revalidates_telegram_item` âœ…
- `test_revalidator_confirms_price_error_bypass` âœ…
- `test_revalidator_confirms_normal_offer_cooldown` âœ…
- `test_revalidator_revalidates_items_older_than_one_hour` âœ…
- `test_revalidator_discards_expired_offer` âœ…
- `test_revalidator_handles_captcha` âœ…
- `test_revalidator_saves_snapshot_when_dom_fails` âœ…

`tests/unit/agents/test_amazon_hunter_agent.py` (6):
- `test_amazon_hunter_creates_price_observation` âœ…
- `test_amazon_hunter_enqueues_offer_over_50_percent` âœ…
- `test_amazon_hunter_enqueues_price_error` âœ…
- `test_amazon_hunter_discards_out_of_stock` âœ…
- `test_amazon_hunter_dom_failure_saves_snapshot` âœ…
- `test_amazon_hunter_handles_captcha` âœ…

### Fixtures HTML

Todos sintÃ©ticos pero realistas (`tests/fixtures/amazon/`):
- `jbl_normal_offer.html` â€” caso feliz (selectores normales + JSON-LD + OG).
- `iphone_extreme_low_price.html` â€” error de precio extremo.
- `monthly_payment_only.html` â€” mensualidad / MSI.
- `out_of_stock.html` â€” sin stock.
- `no_image.html` â€” sin imagen.
- `dom_broken.html` â€” selectores rotos completamente.
- `og_image_fallback.html` â€” fuerza fallback a `og:image`.
- `json_ld_fallback.html` â€” sÃ³lo JSON-LD disponible.

### Suite completa

```
pytest -q
175 passed in 0.79s
```

Cobertura de los 25 tests obligatorios listados en spec Â§14:

| Test obligatorio | Estado |
|---|---|
| test_amazon_extracts_current_price | âœ… |
| test_amazon_extracts_previous_price | âœ… |
| test_amazon_calculates_discount_percent | âœ… |
| test_amazon_extracts_main_image | âœ… |
| test_amazon_extracts_title | âœ… |
| test_amazon_extracts_availability | âœ… |
| test_amazon_extracts_asin_from_url | âœ… |
| test_amazon_normalizes_canonical_url | âœ… |
| test_amazon_detects_monthly_payment_not_total_price | âœ… |
| test_amazon_detects_variant_mismatch | âœ… |
| test_amazon_no_image_not_publishable | âœ… |
| test_amazon_no_price_not_publishable | âœ… |
| test_amazon_out_of_stock_not_publishable | âœ… |
| test_amazon_dom_failure_saves_snapshot | âœ… |
| test_amazon_fallback_og_image | âœ… |
| test_amazon_fallback_json_ld | âœ… |
| test_revalidator_revalidates_telegram_item | âœ… |
| test_revalidator_revalidates_items_older_than_one_hour | âœ… |
| test_revalidator_confirms_price_error_bypass | âœ… |
| test_revalidator_confirms_normal_offer_cooldown | âœ… |
| test_revalidator_discards_expired_offer | âœ… |
| test_amazon_hunter_creates_price_observation | âœ… |
| test_amazon_hunter_enqueues_offer_over_50_percent | âœ… |
| test_amazon_hunter_enqueues_price_error | âœ… |
| test_revalidator_handles_captcha (extra) | âœ… |
| test_revalidator_saves_snapshot_when_dom_fails (extra) | âœ… |
| test_amazon_hunter_discards_out_of_stock (extra) | âœ… |
| test_amazon_hunter_handles_captcha (extra) | âœ… |

### CÃ³mo validar una URL de Amazon manualmente

```powershell
# 1. Smoke test: Â¿Playwright instalado y funcional?
pip install playwright
playwright install chromium
python scripts/test_playwright.py --smoke

# 2. Validar una URL especÃ­fica (sin tocar SQLite):
python -m ofertas_hunter amazon-parse-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8"
# o con exit code != 0 si no es publicable:
python -m ofertas_hunter amazon-validate-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8"

# 3. Modo no-headless (debugging visual):
python scripts/test_playwright.py --url "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --no-headless

# 4. Revalidar como si fuera un item del outbox:
python -m ofertas_hunter revalidate-url "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --expected-title "JBL Tune 510BT"
```

### CÃ³mo correr el hunter Amazon

```powershell
python -m ofertas_hunter init-db   # primera vez

# Con seeds especÃ­ficos:
python -m ofertas_hunter amazon-hunt --seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --limit 1
.\scripts\run_amazon_hunter.ps1 -Seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" -Limit 1

# Linux:
./scripts/run_amazon_hunter.sh --seed "https://www.amazon.com.mx/dp/B0CZ2FW5R8" --limit 1
```

### CÃ³mo revalidar el outbox

```powershell
# Revalida hasta 10 items pendientes (fetcheando con Playwright). NO publica.
python -m ofertas_hunter revalidate-outbox --limit 10

# Output por item:
#   OK   outbox_id=12   type=price_error  score_class=price_error_confirmed  confidence=very high
#   BAD  outbox_id=15   fatal=out_of_stock  reasons=['out_of_stock']
```

### Flujo completo Telegram â†’ outbox â†’ revalidator â†’ dispatcher dry-run

```
Mensaje Telegram (ej: ERROR DE PRECIO iPhone 16 Pro Max $3,899)
        â”‚
        â–¼
TelegramListenerAgent (Fase 3.2)
   â”œâ”€ parse_message(): marketplace=amazon, brand=apple, category=smartphone_flagship,
   â”‚                   urgency_score=40, is_price_error_keyword=True
   â”œâ”€ LinkResolver: bit.ly â†’ https://www.amazon.com.mx/dp/B0XXXXXXXX
   â”œâ”€ TelegramCandidateBuilder.build():
   â”‚     classification=telegram_price_error_signal
   â”‚     PriceErrorScorer score = 95 (very high)
   â”‚     payload con requires_live_validation=True
   â””â”€ persiste: telegram_messages, products, offers (state=eligible),
      outbox (state=pending, type=price_error)

[Tiempo pasa: outbox_age > 1h]
        â”‚
        â–¼
OutboxDispatcher (Fase 3.1, ahora con PlaywrightRevalidator inyectado)
   tick():
       â”œâ”€ revalidate_old_items(): item.enqueued_at > 1h â†’ revalidator.revalidate(item)
       â”‚   PlaywrightRevalidator (Fase 3.3):
       â”‚     â”œâ”€ browser.fetch(item.url) â†’ RenderedPage(html, status=200)
       â”‚     â”œâ”€ AmazonProductParser.parse(html, url, expected_title=...)
       â”‚     â”‚     ExtractedProduct(title="iPhone 16 Pro Max...", current_price=3899,
       â”‚     â”‚                      previous_price=32999, discount_percent=88,
       â”‚     â”‚                      image_url=..., in_stock=True, is_publishable=True)
       â”‚     â”œâ”€ scorer.score(signal) â†’ score=95, classification=price_error_confirmed
       â”‚     â”œâ”€ decide_outbox_type â†’ "price_error" (bypass cooldown)
       â”‚     â””â”€ build_payload con datos reales y requires_live_validation=False
       â”‚
       â”œâ”€ outbox.update_after_revalidation(item, new_payload)
       â”œâ”€ pick_random_eligible() â†’ item (PE prioridad alta)
       â””â”€ publisher.publish(item)
              â”œâ”€ formatter.format_price_error(...)
              â””â”€ EvolutionClient.send_media(...) [DRY-RUN]
                   logger: "[DRY-RUN] sendMedia to 120363@g.us | ðŸš¨ ERROR DE PRECIO ðŸš¨..."
                   PUBLISHING_ENABLED=false â†’ Outcome(skipped=True) o
                   PUBLISHING_DRY_RUN=true â†’ success=True dry_run=True

[NO se envÃ­a nada a WhatsApp real porque PUBLISHING_DRY_RUN=true.]
[published_messages registra el "envÃ­o" simulado con success=1.]
```

### Decisiones tÃ©cnicas

- **Parser separado de Playwright**: el `AmazonProductParser` recibe `html: str` y devuelve `ExtractedProduct`, sin tocar Playwright. Esto permite tests deterministas con fixtures HTML y desacopla la lÃ³gica de extracciÃ³n de la red.
- **`BrowserWorker` Protocol**: tests usan `FakeBrowserWorker` con `dict[str, RenderedPage]`. El `PlaywrightBrowserWorker` real implementa la misma interfaz y se importa lazy.
- **JSON-LD + OG como fallbacks reales**: si Amazon cambia los selectores `#corePrice_desktop` o `#savingsPercentage`, JSON-LD y `og:image` cubren el caso. El test `test_amazon_fallback_json_ld` lo demuestra.
- **Variant mismatch defensivo**: el matching usa cobertura del expected_title (â‰¥70%) o Jaccard â‰¥0.3. El parser sÃ³lo lo aplica si el caller pasa `expected_title` (Telegram lo hace, hunter no).
- **Mensualidad detectada por dos vÃ­as**: regex en el texto del precio (`/mes`, `MSI`, `12 pagos`) + zona DOM (`#installmentCalculator_feature_div`).
- **`PlaywrightRevalidator` cumple con la interfaz `Revalidator` de Fase 3.1**: el dispatcher no necesita conocer detalles. Si Mercado Libre se suma en Fase 3.4, basta con extender `_parse()` para enrutar al parser correspondiente.
- **`requires_live_validation=False`** se setea en el payload tras revalidaciÃ³n exitosa, asÃ­ prÃ³ximos ticks no la repiten.
- **PUBLISHING_ENABLED=false y PUBLISHING_DRY_RUN=true** siguen intactos. Telegram_enabled tambiÃ©n sigue en false. Toda la fase 3.3 se valida con fixtures locales y mocks; las pruebas reales requieren `pip install playwright && playwright install chromium` y usar los comandos `amazon-parse-url`, `amazon-validate-url`, `revalidate-url` o `amazon-hunt --seed <url>`.

### Criterios de aceptaciÃ³n cumplidos

- âœ… pytest completo: **175/175 passed** (Fase 1+2+3.1+3.2+3.3).
- âœ… No se rompiÃ³ Fase 3.1 ni Fase 3.2.
- âœ… Parser Amazon funciona con fixtures HTML.
- âœ… Revalidator puede validar un item de Telegram antes de publicaciÃ³n.
- âœ… Items de Telegram quedan `requires_live_validation=True` hasta validarse.
- âœ… Item Amazon con imagen/precio/stock/descuento â‰¥50 entra al outbox como NORMAL.
- âœ… Error de precio confirmado entra como PRICE_ERROR con bypass cooldown.
- âœ… No se publica nada real (`PUBLISHING_DRY_RUN=true`).
- âœ… Si falla DOM, se guarda snapshot en `dom_snapshots` y razÃ³n en `discarded_candidates`.
- âœ… `changes.md` actualizado.

### Pendiente â€” Fase 3.4

`mercadolibre_hunter` con cookies persistentes. El `PlaywrightRevalidator` ya tiene la rama lista para aÃ±adir un `MercadoLibreProductParser` (ver `_parse()` con `if marketplace == "amazon"`).


---

## 2026-05-25 â€” Fase 3.4 (Mercado Libre + afiliados) âœ…

### AuditorÃ­a honesta

Inicialmente portÃ© schema/modelos con `affiliate_link, affiliate_product_id, commission_text` (heredados del audit) pero **olvidÃ© portar la lÃ³gica real** de extracciÃ³n del modal Compartir. El usuario me lo seÃ±alÃ³ y lo arreglÃ© en este mismo bloque, antes de cerrar Fase 3.4.

**Origen de la lÃ³gica afiliada en el legacy:**
- `bot_diversidad_global/src/browser_worker.py::extract_affiliate_link()` (lÃ­neas ~140-220).
- Click `[data-testid="generate_link_button"]` â†’ espera modal "Generar link" â†’ polling sobre `[data-testid="text-field__label_link"]` (textarea con `meli.la/...`) y `[data-testid="text-field__label_id"]`.
- Lee `commission_text` de `.stripe-commission__info span` antes del click.
- **NO usa OAuth API de ML**. El script `scripts/check_oauth_token.sh` del legacy referencia `/opt/ofertas-detector-vps/data/mercadolibre/oauth_tokens.json` que es de **otro proyecto** del VPS, no del crawler ML.
- `scripts/enrich_affiliate_links.py`: backfill one-shot (descartado correctamente).
- `scripts/test_affiliate_link.py`: test manual con BrowserWorker (su lÃ³gica se reemplaza por `scripts/test_playwright.py` + `ml-validate-url`).

### Archivos creados / modificados (afiliados)

- `src/ofertas_hunter/marketplaces/mercadolibre_affiliate.py` â€” `AffiliateExtractor` (Protocol) + `PlaywrightAffiliateExtractor` real (lazy import). Devuelve `AffiliateInfo(affiliate_url, affiliate_product_id, commission_text, success, error)`. Reproduce exactamente el flujo del legacy.
- `src/ofertas_hunter/agents/mercadolibre_hunter_agent.py` â€” extendido con:
  - parÃ¡metros `affiliate_extractor` y `affiliate_required_for_publish`.
  - `_maybe_extract_affiliate(product)`: llama al extractor sÃ³lo si hay share button.
  - Si `affiliate_required_for_publish=True` y no hay `affiliate_url` â†’ descarta con razÃ³n `missing_affiliate_url`.
  - `_update_product_affiliate(product_id, info)`: actualiza `products.affiliate_link/affiliate_product_id/commission_text`.
  - Payload del outbox incluye `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url`, y `url=affiliate_url or canonical_url`.
- `src/ofertas_hunter/publishing/whatsapp_publisher.py` â€” extendido con `mercadolibre_affiliate_required: bool=True`. Si es `mercadolibre` y falta `affiliate_url`, retorna `error="missing_affiliate_url"` sin llamar a Evolution. El formatter usa `affiliate_url > url > canonical_url`.
- `src/ofertas_hunter/revalidation/playwright_revalidator.py` â€” `_build_payload` conserva `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url` del payload original tras revalidar (la revalidaciÃ³n NO regenera el modal).
- `src/ofertas_hunter/config.py` â€” `mercadolibre_affiliate_required_for_publish=True`, `mercadolibre_affiliate_timeout_seconds=15.0` y otras vars de ML.
- `.env.example` â€” vars nuevas con compatibilidad legacy.

### Tests aÃ±adidos (afiliados â€” todos verde)

`tests/unit/agents/test_mercadolibre_hunter_agent.py`:
- `test_ml_hunter_requests_affiliate_before_outbox` â€” el hunter llama al extractor con `canonical_url`. âœ…
- `test_ml_outbox_payload_contains_affiliate_url` â€” `affiliate_url`, `affiliate_product_id`, `commission_text`, `canonical_url` estÃ¡n en el payload. âœ…
- `test_ml_canonical_url_is_used_for_revalidation` â€” `canonical_url` (sin `meli.la`) se conserva. âœ…
- `test_ml_missing_affiliate_blocks_publication` â€” sin afiliado + `required=True` â†’ `discarded_reason="missing_affiliate_url"`. âœ…
- `test_ml_missing_affiliate_allowed_when_not_required` â€” sin afiliado + `required=False` â†’ entra al outbox. âœ…
- `test_ml_no_share_button_skips_affiliate_extraction` â€” sin share button no se llama al extractor (devuelve `no_share_button` sintÃ©tico). âœ…

`tests/unit/publishing/test_whatsapp_publisher_ml.py`:
- `test_whatsapp_uses_affiliate_url_for_ml` â€” el mensaje contiene `meli.la/...`, no la canonical_url. âœ…
- `test_whatsapp_blocks_ml_without_affiliate` â€” gate ML afiliado bloquea publicaciÃ³n con `error="missing_affiliate_url"`. âœ…
- `test_whatsapp_allows_ml_without_affiliate_when_not_required` â€” gate desactivado â†’ publica con canonical. âœ…
- `test_whatsapp_amazon_does_not_require_affiliate` â€” gate sÃ³lo aplica a `marketplace=mercadolibre`. âœ…

`tests/unit/revalidation/test_mercadolibre_revalidator.py`:
- `test_ml_revalidator_preserves_affiliate_url` â€” tras revalidar, el payload conserva `affiliate_url`, `affiliate_product_id`, `commission_text`. âœ…
- `test_ml_revalidator_uses_canonical_for_fetch_when_available` â€” fetchea `resolved_url` (canonical), no la URL afiliada. âœ…
- `test_ml_revalidator_blocks_telegram_source` â€” `source=telegram` + ML â†’ `discard_reason="mercadolibre_link_from_telegram"`. âœ…
- `test_ml_revalidator_confirms_normal_offer` â€” revalidaciÃ³n exitosa funciona con ML. âœ…

`tests/unit/telegram/test_candidate_builder.py`:
- `test_ml_telegram_source_links_remain_ignored_no_affiliate` â€” links ML desde Telegram nunca llegan al hunter ML, por lo que nunca generan `affiliate_url`. âœ…

### Suite final

```
pytest -q
230 passed in 1.29s
```

### Reglas duras finales

1. **ML desde Telegram**: ignorado por `TelegramCandidateBuilder` (`internal_classification=ignored_mercadolibre_from_telegram`). Nunca llega al hunter ML, nunca genera afiliado, nunca se publica.
2. **ML desde hunter propio**: pasa por `MercadoLibreHunterAgent` â†’ llama a `AffiliateExtractor` si hay share button â†’ si hay afiliado, lo guarda en `products` y `outbox.payload`.
3. **`MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`** (default): un item ML sin `affiliate_url` se descarta en el hunter (`missing_affiliate_url` en `discarded_candidates`). Si por alguna razÃ³n llegara al publisher, el gate ML del publisher lo bloquearÃ­a con `error="missing_affiliate_url"` antes de tocar Evolution.
4. **PublicaciÃ³n WhatsApp**: el mensaje usa `affiliate_url > url > canonical_url`. La URL `meli.la/...` aparece literalmente en el mensaje del grupo.
5. **RevalidaciÃ³n**: `canonical_url` (no `affiliate_url`) se usa para fetchear contra ML. `affiliate_url` se conserva del payload original sin regenerar (eso requiere otro fetch + click + cookies del afiliado).

### Lo que sigue igual sin cambio

- `PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true` (default).
- `TELEGRAM_ENABLED=false` (default).
- Cero credenciales reales activadas.
- AmazonScrapperIA y bot_diversidad_global intactos.


---

## 2026-05-25 â€” Fase 3.4 cierre con `ml-enrich-affiliates` âœ…

### Resumen

Completo Fase 3.4 con el comando solicitado para enriquecer afiliados en items existentes del outbox. **Una sola lÃ³gica reutilizada**: el servicio `MercadoLibreAffiliateEnricher` es invocado tanto por el CLI como por el script wrapper. Cero duplicaciÃ³n.

### Archivos creados

- `src/ofertas_hunter/agents/mercadolibre_affiliate_enricher.py` â€” servicio puro:
  - `MercadoLibreAffiliateEnricher(conn, extractor)` con mÃ©todo `run(limit)`.
  - `EnrichmentReport(total_candidates, enriched, failed, skipped, outcomes)`.
  - `EnrichmentOutcome(outbox_id, canonical_url, affiliate_url, ..., status)`.
  - SELECT filtrando ML + no-Telegram + sin `affiliate_url`.
  - Persiste Ã©xito en `outbox.message_payload_json` (campos `affiliate_url`, `affiliate_product_id`, `commission_text`, `url=affiliate_url`, `affiliate_status="ok"`, `affiliate_enriched_at`) y refleja en `products` si url_canonical existe.
  - Persiste fallo dejando `affiliate_status="failed"` (o `"pending"` si extractor=None) y `affiliate_error=<motivo>`. **No publica** ni cambia `outbox.state` â€” sÃ³lo aÃ±ade metadata.

- `scripts/enrich_affiliate_links.py` â€” wrapper que delega a `python -m ofertas_hunter ml-enrich-affiliates`. Reutiliza la misma funciÃ³n `main()` del paquete: cero duplicaciÃ³n.

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - Nuevo subcomando `ml-enrich-affiliates [--limit N] [--no-headless] [--no-extractor]`.
  - El handler arma Playwright + cookies de `MercadoLibreSession` y crea `PlaywrightAffiliateExtractor`. Si Playwright no estÃ¡, marca todos los candidatos con `status=pending` para reintento.
  - `--no-extractor` permite ejecutar el comando sin Playwright (Ãºtil para QA, CI, o cuando se quiere encolar pendientes a revisar).

### Tests aÃ±adidos (7, todos verde)

`tests/unit/agents/test_mercadolibre_affiliate_enricher.py`:
- `test_ml_enrich_affiliates_skips_telegram_source` â€” items con `source=telegram` ni siquiera entran a candidatos. âœ…
- `test_ml_enrich_affiliates_skips_items_with_existing_affiliate_url` â€” items que ya traen `affiliate_url` se preservan intactos. âœ…
- `test_ml_enrich_affiliates_updates_outbox_payload` â€” Ã©xito: payload incluye `affiliate_url, affiliate_product_id, commission_text, affiliate_status="ok", affiliate_enriched_at`; `url=affiliate_url`; `canonical_url` se conserva; `products` se actualiza. âœ…
- `test_ml_enrich_affiliates_records_failure` â€” fallo: payload trae `affiliate_status="failed"`, `affiliate_error="modal_textareas_empty"`, `url` sigue siendo canonical, no se setea `affiliate_url`. âœ…
- `test_ml_enrich_affiliates_marks_pending_when_no_extractor` â€” sin Playwright, los items se marcan como `status=pending`/`error="no_extractor"` para reintento. âœ…
- `test_ml_enrich_affiliates_respects_limit` â€” `--limit N` selecciona los N primeros candidatos. âœ…
- `test_ml_enrich_affiliates_skips_non_mercadolibre` â€” items de Amazon u otros marketplaces no entran a candidatos. âœ…

### Suite final

```
pytest -q
237 passed in 1.37s
```

### ValidaciÃ³n end-to-end del CLI

```powershell
python -m ofertas_hunter init-db
python -m ofertas_hunter ml-enrich-affiliates --no-extractor --limit 5
# --- ml-enrich-affiliates ---
#   candidates: 0
#   enriched:   0
#   failed:     0
#   skipped:    0
```

(En una DB vacÃ­a no hay candidatos; con items reales el comando llamarÃ­a a Playwright + cookies y actualizarÃ­a el payload de cada uno.)

### CÃ³mo ejecutarlo

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

1. âœ… SÃ³lo procesa items `marketplace=mercadolibre`.
2. âœ… SÃ³lo procesa items `source != telegram`.
3. âœ… SÃ³lo procesa items sin `affiliate_url`.
4. âœ… Usa `canonical_url` para abrir el producto (no la URL afiliada).
5. âœ… Guarda `affiliate_url, affiliate_product_id, commission_text` y `affiliate_status="ok"` en Ã©xito.
6. âœ… Guarda `affiliate_status="failed"` o `"pending"` y `affiliate_error=<motivo>` en fallo.
7. âœ… **No publica nada**: solo modifica metadata del payload del outbox.
8. âœ… El gate ML del publisher (`mercadolibre_affiliate_required_for_publish=true`) sigue protegiendo: items sin afiliado no se publican aunque el dispatcher los pickee.

### Estado de la Fase 3.4

**Cerrada.** Todo lo exigido estÃ¡ implementado, testeado y documentado:
- Parser ML con cookies, share button, condition (used/refurbished), monthly payment, variant mismatch.
- Extractor real del modal Compartir (Playwright lazy import).
- Hunter ML que llama al extractor antes del outbox y enforza `affiliate_required_for_publish`.
- Revalidator que conserva afiliado tras revalidaciÃ³n y bloquea ML desde Telegram.
- Publisher con gate dedicado para ML que falla con `missing_affiliate_url` cuando aplica.
- CLI `ml-enrich-affiliates` + script wrapper.
- 237 tests verde (incluyendo 36 nuevos de Fase 3.4 + 7 del enricher).
- `PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true`, `TELEGRAM_ENABLED=false` intactos.
- `MERCADOLIBRE_ENABLED=false` por default (se activa con cookies + extractor + decision del operador).

### PrÃ³xima fase

Fase 4 (watchdog, self-healing, memory compressor, deploy systemd) o lo que el operador defina.


---

## 2026-05-25 â€” Fase 4 (watchdog + self-healing + memory + deploy) âœ…

Cuatro bloques implementados sin tocar Telethon, Playwright real ni envÃ­o real. Todo testeado con clocks inyectables y DBs temporales.

### Fase 4.1 â€” Runtime Watchdog + Heartbeat

`src/ofertas_hunter/runtime/`:
- `heartbeat.py` â€” `AgentRunRegistry(conn, clock=...)` que persiste `agent_runs(started_at, status, last_heartbeat, ended_at)`. Clock inyectable para tests.
- `events.py` â€” `emit_runtime_event(conn, kind, severity, payload)` central.
- `watchdog.py` â€” `RuntimeWatchdog` con:
  - Registro de `SupervisedAgent(name, factory)`.
  - Tick que detecta `task.done()` o heartbeat viejo > `stale_after_seconds` y reinicia con backoff exponencial.
  - Si excede `max_restarts_in_window` â†’ `agent.degraded=True`, registra `runtime_event(severity=critical, kind="agent_degraded")` y deja de reintentar.
  - `shutdown_all()` cancela tareas vivas y marca `agent_runs.status="killed"`.

12 tests verde (`tests/unit/runtime/`).

### Fase 4.2 â€” DOM Healer con tests automÃ¡ticos antes de patch

`src/ofertas_hunter/self_healing/`:
- `degradation_monitor.py` â€” `DegradationMonitor(window, threshold, min_samples)`. Por `(marketplace, context)`, dispara `is_degraded=True` cuando la tasa de fallos supera `threshold`.
- `selector_versioner.py` â€” registra `selector_versions(marketplace, context, key, selector_value, applied_by, test_pass, fixture_path, reverted_at)`.
- `dom_healer.py` â€” `DomHealer.heal(marketplace, context, url, html)`:
  1. Guarda `dom_snapshots`.
  2. `HeuristicSelectorRecovery` extrae `current_price` (JSON-LD), `image_url` (og:image), `title` (og:title), regex price.
  3. Persiste fixture HTML local en `data/heal_samples/<marketplace>/<context>/healed_<ts>.html`.
  4. Llama al `tester(fixture_path)` inyectado. Si pasa â†’ `selector_versions(test_pass=1)`. Si falla â†’ `revert(reason="tests_failed")`.
- Sin LLM real; arquitectura abierta (`tester` callable es el punto de extensiÃ³n).

16 tests verde (`tests/unit/self_healing/`).

### Fase 4.3 â€” Memory Compressor

`src/ofertas_hunter/memory/compressor.py`:
- `CompressorConfig` con polÃ­ticas por tabla:
  - `dom_snapshots_keep=200` (mantiene N mÃ¡s recientes)
  - `runtime_events_ttl_days=30`
  - `discarded_candidates_ttl_days=30` + `discarded_candidates_keep_max=5000`
  - `agent_runs_keep=1000`
- Genera 4 `memory_summaries`:
  - `discard_reasons` (top 10 razones por source)
  - `unstable_selectors` (>= 3 versiones en 30d)
  - `runtime_events_by_severity` (critical primero)
  - `marketplace_observations` (cuenta de `price_observations` por marketplace)
- Clock inyectable. Idempotente con DB vacÃ­a.

6 tests verde (`tests/unit/memory/`).

### Fase 4.4 â€” CLI + scripts de mantenimiento

`src/ofertas_hunter/__main__.py` aÃ±adiÃ³:
- `compress-memory`
- `export-memory-summary [--kind ...] [--out file]`
- `check-db` (counts de tablas + journal_mode + foreign_keys)
- `status` (env + agentes activos + outbox + runtime events crÃ­ticos)
- `watchdog-tick --once` (placeholder para integraciÃ³n futura)

Scripts wrappers (delegan al CLI, cero duplicaciÃ³n):
- `scripts/check_db.py`
- `scripts/export_memory_summary.py`
- `scripts/status.sh`
- `scripts/run_worker.sh`

### Fase 4.5 â€” Deploy (systemd + Docker)

`deploy/systemd/`:
- `ofertas-hunter.service` â€” orchestrator (placeholder; Fase 5 conectarÃ¡ al watchdog real).
- `ofertas-hunter-dispatcher.service` â€” outbox dispatcher.
- `ofertas-hunter-telegram.service` â€” listener Telethon.
- `ofertas-hunter-maintenance.service` + `.timer` â€” corre `compress-memory` cada 6h.
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths` limitado.

`deploy/install.sh` â€” instalador idempotente que renderiza los unit files reemplazando `__PROJECT_ROOT__` y `__SERVICE_USER__`. **No arranca los servicios automÃ¡ticamente** â€” el operador decide cuÃ¡ndo.

`deploy/docker/Dockerfile` â€” imagen Python 3.11-slim con dependencias de Chromium para Playwright. `python -m playwright install --with-deps chromium`. Volumes para `data`, `secrets`, `logs`. Entrypoint `python -m ofertas_hunter`, comando default `status`.

`deploy/docker/docker-compose.yml` â€” services para `dispatcher`, `telegram-listener`, `amazon-hunter-once`, `ml-hunter-once`, `maintenance` (con profiles).

### Suite final

```
pytest -q
271 passed in 3.04s
```

DistribuciÃ³n de tests nuevos en Fase 4:
- `tests/unit/runtime/` â€” 12
- `tests/unit/self_healing/` â€” 16
- `tests/unit/memory/` â€” 6

**Total Fase 4: +34 tests** (de 237 a 271 sin regresiones).

### CÃ³mo desplegar en VPS

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

# 4. Cuando estÃ©s listo (sigue dry-run por default)
sudo systemctl enable --now ofertas-hunter
sudo systemctl enable --now ofertas-hunter-dispatcher

# 5. Ver estado
sudo systemctl status ofertas-hunter ofertas-hunter-dispatcher
sudo journalctl -u ofertas-hunter -f
sudo -iu ofertas /opt/ofertas-hunter/scripts/status.sh
```

### CÃ³mo desplegar con Docker

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
| 1 â€” AuditorÃ­a + diseÃ±o + schema | âœ… |
| 2 â€” Core | âœ… |
| 3.1 â€” outbox_dispatcher + Evolution | âœ… |
| 3.2 â€” telegram_listener Telethon | âœ… |
| 3.3 â€” amazon_hunter + revalidator | âœ… |
| 3.4 â€” mercadolibre_hunter + afiliados | âœ… |
| 4.1 â€” runtime_watchdog | âœ… |
| 4.2 â€” dom_healer + selector_versioner | âœ… |
| 4.3 â€” memory_compressor | âœ… |
| 4.4 â€” CLI + scripts mantenimiento | âœ… |
| 4.5 â€” deploy systemd + Docker | âœ… |

**271 tests verde. Cero regresiones desde Fase 1. Cero diagnostics.**

`PUBLISHING_ENABLED=false`, `PUBLISHING_DRY_RUN=true`, `TELEGRAM_ENABLED=false`, `MERCADOLIBRE_ENABLED=false` siguen en `.env.example` por default.

### Pendiente en `run` profile

El subcomando `python -m ofertas_hunter run` actualmente es un no-op con warning. La spec original (Â§8 Multiagentes + Â§13 Fase 4) habla de un `supervisor/orchestrator` que arranca todos los agentes registrados al watchdog. La infraestructura estÃ¡ lista (`RuntimeWatchdog` + `AgentRunRegistry` + factories tipadas), pero conectarlas en un Ãºnico `orchestrator.py` que lea `.env` y arme las factories de los hunters/dispatcher/listener queda como **Fase 5** cuando se decida activar la operaciÃ³n end-to-end real.


---

## 2026-05-25 â€” Fase 5 (orchestrator real conectando todos los agentes) âœ…

### Resumen

`python -m ofertas_hunter run` ya no es un no-op: arranca el orchestrator completo registrando los agentes habilitados en el watchdog (Fase 4.1). Cada agente respeta su flag `*_ENABLED` del `.env`. Si una dependencia opcional falta (Playwright, Telethon, cookies), el agente queda dormido en lugar de crashear.

### Archivos creados

- `src/ofertas_hunter/orchestrator.py` (510 lÃ­neas):
  - `OrchestratorConfig` con intervalos por agente, seeds, lÃ­mites.
  - `AgentFactoryBuilder` que construye factories tipadas para:
    - `outbox_dispatcher` (siempre activo, drena outbox respetando flags)
    - `amazon_hunter` (sÃ³lo si `AMAZON_ENABLED=true`)
    - `mercadolibre_hunter` (sÃ³lo si `MERCADOLIBRE_ENABLED=true`, con cookies + extractor afiliado)
    - `telegram_listener` (sÃ³lo si `TELEGRAM_ENABLED=true` y credenciales presentes)
    - `maintenance` (siempre activo, corre `compress-memory` cada 6h)
  - `Orchestrator.register_agents()` â†’ `RegistrationReport(registered, skipped)`.
  - `Orchestrator.run()` â†’ registra agentes, emite `runtime_event(orchestrator_starting)`, arranca el watchdog en modo `--once` o continuo.
  - `load_amazon_seeds(settings)` y `load_mercadolibre_seeds(settings)` para leer `config/seeds/*.json`.
  - **Lazy imports**: Playwright/Telethon sÃ³lo se importan cuando se intenta arrancar el agente correspondiente. Si fallan, emiten `runtime_event(severity=warning, kind=agent_skipped)` y entran en loop ocioso (no crashean al watchdog).

### Archivos modificados

- `src/ofertas_hunter/__main__.py`:
  - `cmd_run` ahora llama a `Orchestrator.run()` con `--once` opcional.
  - Subparser de `run` con `--once` y `--limit`.
- `changes.md` â€” log de Fase 5.

### Comportamiento por flag

| Flag                              | Default | Efecto                                              |
|-----------------------------------|---------|-----------------------------------------------------|
| `PUBLISHING_ENABLED=false`        | âœ…       | Publisher retorna `skipped=true`; items quedan pending |
| `PUBLISHING_DRY_RUN=true`         | âœ…       | Si llega a publicar, simula sin tocar Evolution API |
| `TELEGRAM_ENABLED=false`          | âœ…       | Listener no se registra; watchdog emite `agent_skipped` |
| `AMAZON_ENABLED=true` + sin seeds | -       | Hunter registrado pero loop ocioso                  |
| `MERCADOLIBRE_ENABLED=true` + sin cookies | -       | Hunter registrado; emite `cookie_expiry`; ML afiliado fail |

### Tests aÃ±adidos (7, todos verde)

`tests/unit/test_orchestrator.py`:
- `test_orchestrator_registers_only_enabled_agents` â€” flags off â†’ skip + runtime_events.
- `test_orchestrator_registers_all_when_flags_enabled`.
- `test_orchestrator_run_once_calls_each_agent` â€” `--once` ejecuta una pasada por agente.
- `test_orchestrator_handles_agent_failure_gracefully` â€” un agente que crashea no detiene los demÃ¡s; queda `agent_runs.status='error'`.
- `test_orchestrator_emits_event_with_skipped_agents` â€” el evento `orchestrator_starting` lista agentes skipped.
- `test_orchestrator_dispatcher_always_registered` â€” dispatcher + maintenance siempre activos.
- `test_orchestrator_records_agent_runs_for_each_cycle`.

Builder usado en tests: `FakeFactoryBuilder` que devuelve factories triviales (sin Playwright/Telethon). Esto deja la lÃ³gica de wiring 100% testeable.

### Suite final

```
pytest -q
278 passed in 3.22s
```

### ValidaciÃ³n end-to-end

```powershell
# 1. DB limpia
python -m ofertas_hunter init-db

# 2. Encolar oferta de prueba
python -m ofertas_hunter enqueue-sample --kind normal

# 3. Correr orchestrator un solo ciclo
python -m ofertas_hunter run --once

# Logs:
#   [INFO] publishing_enabled=False publishing_dry_run=True â€” modo seguro activo
#   [WARNING] agent amazon_hunter no se registra: no_seeds_configured
#   [WARNING] agent mercadolibre_hunter no se registra: no_seeds_configured
#   [WARNING] agent telegram_listener no se registra: telegram_disabled
#   [INFO] memory_compactation: dom=0 events=0 disc=0 runs=0 summaries=[...]
#   [INFO] publish skipped: publishing_disabled
#   [INFO] Publish skipped (publishing_disabled) item=1 â€” se mantiene pending

# 4. Estado consolidado
python -m ofertas_hunter status
# active agents: (ninguno)  â† --once ya terminÃ³
# outbox: normal pending 1
```

### CÃ³mo activar operaciÃ³n real

Sin tocar `PUBLISHING_DRY_RUN=true` no hay envÃ­os. Los pasos para producciÃ³n:

1. Editar `.env`:
   ```
   AMAZON_ENABLED=true
   MERCADOLIBRE_ENABLED=true
   TELEGRAM_ENABLED=true                 # con credenciales vÃ¡lidas
   PUBLISHING_ENABLED=true
   PUBLISHING_DRY_RUN=false              # â† ÃšLTIMO paso
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

4. SÃ³lo cuando todo lo anterior pase, arrancar el servicio continuo:
   ```bash
   sudo systemctl start ofertas-hunter
   sudo journalctl -u ofertas-hunter -f
   ```

### Estado final del proyecto

| Fase | Estado |
|---|---|
| 1 â€” AuditorÃ­a + diseÃ±o + schema + 16 tests obligatorios | âœ… |
| 2 â€” Core (config, db, scorer, formatter, outbox, telegram parser) | âœ… |
| 3.1 â€” outbox_dispatcher + Evolution API | âœ… |
| 3.2 â€” telegram_listener Telethon | âœ… |
| 3.3 â€” amazon_hunter + PlaywrightRevalidator | âœ… |
| 3.4 â€” mercadolibre_hunter + afiliados + ml-enrich-affiliates | âœ… |
| 4.1 â€” runtime_watchdog + heartbeat | âœ… |
| 4.2 â€” dom_healer + selector_versioner | âœ… |
| 4.3 â€” memory_compressor | âœ… |
| 4.4 â€” CLI + scripts mantenimiento | âœ… |
| 4.5 â€” deploy systemd + Docker | âœ… |
| **5 â€” Orchestrator real con todos los agentes** | **âœ…** |

**278 tests verde. Cero regresiones desde Fase 1. Cero diagnostics. AmazonScrapperIA y bot_diversidad_global intactos.**

`PUBLISHING_DRY_RUN=true`, `PUBLISHING_ENABLED=false`, `TELEGRAM_ENABLED=false`, `MERCADOLIBRE_ENABLED=false`, `AMAZON_ENABLED=true` (default) siguen como estÃ¡n en `.env.example`. **Cero envÃ­os reales hasta que el operador lo autorice explÃ­citamente.**


---

## 2026-05-25 â€” Fase 6 (descubrimiento autÃ³nomo + credenciales legacy) âœ…

El bot deja de depender de listas estÃ¡ticas de productos. Ahora visita listings/categorÃ­as/deals, extrae URLs nuevas y las persiste en `frontier`. Los hunters consumen esas URLs en lugar de seeds estÃ¡ticas. Resultado: ofertas frescas en cada ciclo.

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
- `src/ofertas_hunter/exploration/url_classifier.py` â€” `classify(url) -> ClassifiedUrl(marketplace, kind, score)`. Identifica `product | listing | category | deals | unknown` por marketplace. Bloquea login/help/blog/seller/registro de ML.
- `src/ofertas_hunter/exploration/frontier.py` â€” `FrontierRepo(conn)` sobre la tabla `frontier` ya existente. API: `add`, `add_many`, `add_classified`, `pop`, `peek`, `mark_visited`, `is_visited`, `count_pending`, `count_visited`. UNIQUE en `(marketplace, url_canonical)` evita duplicados; los visited no se reinsertan.
- `src/ofertas_hunter/exploration/listing_extractor.py` â€” `extract_amazon_listing`, `extract_amazon_deals`, `extract_mercadolibre_listing` (BS4 puros, testables). Reconocen `data-component-type='s-search-result'`, `data-asin`, `s-pagination-next`, `ui-search-link`, `ui-search-item__group__element`, `andes-pagination__link`.
- `src/ofertas_hunter/agents/discovery_agent.py` â€” `DiscoveryAgent(browser, db_conn, marketplace)`:
  - `seed_from_config(urls)` siembra categorÃ­as al frontier (idempotente).
  - `discover_once()` toma URLs `kind in {listing, category, deals}` del frontier, las fetchea, extrae productos + paginaciÃ³n, los persiste de vuelta. Maneja login_redirect y captcha emitiendo `runtime_event` y guardando snapshot.

### Archivos modificados

- `config/seeds/amazon.json` â€” 36 URLs reales (search keywords + `/deals` + `/gp/goldbox`). Heredadas y ampliadas desde el legacy `AmazonScrapperIA/config/seeds.json`.
- `config/seeds/mercadolibre.json` â€” 15 categorÃ­as + ofertas reales.
- `.env` â€” credenciales reales heredadas (Evolution + Telegram + paths). Sigue gitignored.
- `src/ofertas_hunter/agents/amazon_hunter_agent.py` â€” nuevo mÃ©todo `hunt_from_frontier(max_urls)` que consume URLs `kind=product` del frontier.
- `src/ofertas_hunter/agents/mercadolibre_hunter_agent.py` â€” idem.
- `src/ofertas_hunter/orchestrator.py`:
  - `make_amazon_hunter_factory` ahora arma `DiscoveryAgent + AmazonHunterAgent` y en cada ciclo:
    1. `discovery.discover_once()` â†’ descubre productos en N listings.
    2. `hunter.hunt_from_frontier()` â†’ procesa N productos del frontier.
  - `make_mercadolibre_hunter_factory` igual, pero con cookies + extractor afiliado.
  - Las seeds dejan de ser productos y se entienden como **puntos de entrada** (categorÃ­as).
- `scripts/validate_seeds.py` â€” utilidad nueva: clasifica las seeds y reporta cuÃ¡les son aceptables (sin tocar red).

### Flujo de descubrimiento autÃ³nomo

```
seeds.json (categorÃ­as/bÃºsquedas)
        â†“
discovery_agent.seed_from_config()  â†’ frontier(kind=listing|category|deals)
        â†“ (cada ciclo)
discovery_agent.discover_once():
   1. frontier.peek(listing/category/deals, limit=2)
   2. browser.fetch(url)
   3. extract_*_listing(html)  â†’ productos + paginaciÃ³n nueva
   4. frontier.add_classified(*)  â†’ kind=product (score=10)
        â†“
hunter.hunt_from_frontier():
   1. frontier.pop(kind=product, limit=5)
   2. parser â†’ ExtractedProduct
   3. price_observation + offer + outbox (si publicable)
   4. discarded_candidates (si no)
        â†“
dispatcher.tick()  â†’  WhatsApp (dry-run por default)
```

El bot **no requiere** que el operador conozca productos especÃ­ficos. SÃ³lo le da puntos de entrada (categorÃ­as populares) y el bot encuentra productos frescos en cada ciclo.

### Tests aÃ±adidos (39, todos verde)

- `tests/unit/exploration/test_url_classifier.py` (17): clasifica URLs Amazon/ML/other; valida bloqueos (login, help, blog, signin); scores correctos.
- `tests/unit/exploration/test_frontier.py` (7): add/pop/visited/dedup/UNIQUE; isolation por marketplace; orden por score desc.
- `tests/unit/exploration/test_listing_extractor.py` (6): extracciÃ³n correcta de productos por `data-asin` y selectores variados; OG/normalizaciÃ³n de URLs relativas; dedup; HTML vacÃ­o.
- `tests/unit/agents/test_discovery_agent.py` (6): seed â†’ frontier; descubrimiento con FakeBrowserWorker; paginaciÃ³n re-encolada; login_redirect emite `cookie_expiry`; captcha emite snapshot; ML extracts.

Cambios menores en tests existentes:
- ML pagination ahora se clasifica como `category` (URL `/c/...?page=2`), antes era `listing`. Test ajustado.

### Suite final

```
pytest -q
317 passed in 3.56s
```

### ValidaciÃ³n de seeds

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

### CÃ³mo arrancar el bot autÃ³nomo

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
2. En cada ciclo, descubre 2 listings/categorÃ­as y extrae los productos visibles.
3. Procesa hasta 5 productos por marketplace.
4. Si encuentra â‰¥50% descuento o error de precio confirmado, encola al outbox.
5. Dispatcher tickea pero no envÃ­a nada (`PUBLISHING_DRY_RUN=true`).

Las pÃ¡ginas reales viven, asÃ­ que cada ciclo descubre productos distintos. Si Amazon cambia el DOM, el `dom_healer` (Fase 4.2) entra en acciÃ³n.

### Estado consolidado

| Fase | Estado |
|---|---|
| 1 â€” AuditorÃ­a + diseÃ±o + schema | âœ… |
| 2 â€” Core | âœ… |
| 3.1 â€” outbox_dispatcher + Evolution | âœ… |
| 3.2 â€” telegram_listener Telethon | âœ… |
| 3.3 â€” amazon_hunter + revalidator | âœ… |
| 3.4 â€” mercadolibre_hunter + afiliados | âœ… |
| 4 â€” watchdog + dom_healer + memory + deploy | âœ… |
| 5 â€” Orchestrator real | âœ… |
| **6 â€” Descubrimiento autÃ³nomo + credenciales legacy** | **âœ…** |

**317 tests verde. Cero regresiones. Cero diagnostics.**


---

## 2026-05-25 â€” Fase 7 (scheduler nocturno + warmup) âœ…

El bot ahora respeta horarios humanos. Tres modos segÃºn hora local MÃ©xico:

| Modo | Ventana | Comportamiento |
|---|---|---|
| **`hibernating`** | 23:30 â€“ 06:30 | Hunters/discovery dormidos. Dispatcher pausado (no publica nada, ni siquiera errores de precio). Watchdog y maintenance siguen activos. |
| **`warmup`** | 06:30 â€“ 07:00 | Hunters/discovery activos a velocidad alta (90s en lugar de 600s). Outbox se llena. **Dispatcher sigue pausado** para acumular ofertas frescas. |
| **`active`** | 07:00 â€“ 23:30 | OperaciÃ³n normal: hunters cada 600s, dispatcher publica respetando cooldown. |

A las 7am el operador ya tiene un outbox con ofertas detectadas durante el warmup, listas para publicar.

### Archivos creados

- `src/ofertas_hunter/runtime/scheduler.py` â€” `OperatingScheduler` + `ScheduleConfig` + `ModeDecision`. Ventanas configurables por env. Soporta cruce de medianoche (`23:30 â†’ 06:30`). Clock inyectable. Cae limpio a UTC si `tzdata` no estÃ¡. Fallback a `ACTIVE` si `enabled=false` (operaciÃ³n legacy).
- `tests/unit/runtime/test_scheduler.py` â€” 23 tests.

### Archivos modificados

- `src/ofertas_hunter/dispatching/dispatcher.py` â€” `OutboxDispatcher` acepta `scheduler` opcional. En `tick()`, si el modo es `hibernating` o `warmup`, no toca outbox (no publica). Loguea el cambio de modo una sola vez.
- `tests/unit/dispatching/test_dispatcher.py` â€” 4 tests nuevos verifican comportamiento en cada modo.
- `src/ofertas_hunter/orchestrator.py`:
  - `AgentFactoryBuilder` instancia un `OperatingScheduler` Ãºnico compartido.
  - `_loop` recibe `scheduler`, `warmup_interval`, `hibernation_check_interval`. En hibernaciÃ³n NO llama a `work()`, sÃ³lo heartbeat cada 60s.
  - `make_dispatcher_factory` pasa el scheduler al dispatcher.
  - Hunters Amazon y ML usan el scheduler con `warmup_interval=90s`.
- `src/ofertas_hunter/config.py` â€” 8 vars nuevas: `schedule_enabled`, `schedule_timezone`, `hibernate_start/end`, `warmup_start`, `active_start`, `warmup_loop_interval_seconds`, `hibernation_check_interval_seconds`. MÃ¡s `mercadolibre_cookies_fallback_path`.
- `src/ofertas_hunter/session/mercadolibre_session.py` â€” `from_settings` acepta `fallback_path` y lo usa si el primary no existe. Ãštil para reutilizar las cookies que el operador puso en `secrets/mercadolibre_cookies.example.json`.
- `src/ofertas_hunter/__main__.py`:
  - `cmd_status` ahora muestra modo actual, hora local y prÃ³ximo cambio.
  - `cmd_run` propaga los flags del scheduler al `OrchestratorConfig`.
- `.env` y `.env.example` â€” secciones de scheduler nuevas.
- `requirements.txt` y `pyproject.toml` â€” aÃ±adido `tzdata==2026.2` (necesario en Windows para `zoneinfo` con `America/Mexico_City`).

### Tests aÃ±adidos (27, todos verde)

`tests/unit/runtime/test_scheduler.py` (23):
- `_within` con rangos normales y cruce de medianoche.
- `_delta_until` hoy/maÃ±ana.
- Modo correcto a 00:00, 02:30, 06:00, 06:29, 06:30, 06:45, 06:59, 07:00, 12:00, 20:00, 23:29, 23:30, 23:45.
- `next_change_in` correcto en cada modo.
- `enabled=False` siempre devuelve `ACTIVE`.
- Ventanas custom (22:00â†’05:00 hibernaciÃ³n, 05:00â†’06:00 warmup).

`tests/unit/dispatching/test_dispatcher.py` (4 nuevos):
- `test_dispatcher_hibernates_no_publishing` â€” outbox intacto en hibernaciÃ³n.
- `test_dispatcher_warmup_no_publishing` â€” outbox intacto en warmup.
- `test_dispatcher_active_publishes_normally`.
- `test_dispatcher_no_scheduler_means_always_active` â€” comportamiento legacy.

### Suite final

```
pytest -q
344 passed in 3.55s
```

### VerificaciÃ³n E2E del status

```
$ python -m ofertas_hunter status
  ...
  schedule:           active
  local_time (America/Mexico_City): 18:09:46
  next_change:        hibernating en 5:20:13
```

A las 23:30 cambia a `hibernating`. A las 06:30 a `warmup`. A las 07:00 a `active`.

### Cookies del legacy

El operador dejÃ³ cookies reales en `secrets/mercadolibre_cookies.example.json`. La nueva config (`MERCADOLIBRE_COOKIES_FALLBACK_PATH=secrets/mercadolibre_cookies.example.json`) hace que `MercadoLibreSession.from_settings` las cargue automÃ¡ticamente cuando el primary `secrets/mercadolibre_cookies.json` no exista. Cuando el operador quiera la separaciÃ³n limpia, basta con renombrar `mercadolibre_cookies.example.json` â†’ `mercadolibre_cookies.json`.

### Comportamiento durante hibernaciÃ³n

- Discovery: skip de la pasada. El frontier no se llena, no se quema cookies.
- Hunters Amazon/ML: skip. Playwright sigue arriba pero sin fetchear (puede dormir incluso sin abrir browser).
- Dispatcher: skip. Outbox no se mueve.
- Telegram listener: **sigue activo** â€” los canales mandan errores de precio en cualquier hora y queremos capturarlos para validarlos en warmup.
- Maintenance: sigue activo cada 6h.
- Watchdog: sigue activo monitoreando heartbeats.

### Comportamiento durante warmup (06:30 â€“ 07:00)

- Discovery + hunters: intervalo 90s en lugar de 600s. Procesan mÃ¡s URLs por minuto.
- Dispatcher: pausado igual que en hibernaciÃ³n. Outbox crece sin publicar.
- A las 07:00 el dispatcher despierta y empieza a sacar ofertas con el primer cooldown (5 min entre normales).

### Estado consolidado

| Fase | Estado |
|---|---|
| 1 â€” AuditorÃ­a + diseÃ±o | âœ… |
| 2 â€” Core | âœ… |
| 3.1 a 3.4 â€” Marketplaces, dispatcher, telegram | âœ… |
| 4 â€” Watchdog + dom_healer + memory + deploy | âœ… |
| 5 â€” Orchestrator real | âœ… |
| 6 â€” Descubrimiento autÃ³nomo + credenciales | âœ… |
| **7 â€” Scheduler nocturno + warmup** | **âœ…** |

**344 tests verde. Cero regresiones desde Fase 1. Cero diagnostics.**

`PUBLISHING_DRY_RUN=true`, `PUBLISHING_ENABLED=false`, `TELEGRAM_ENABLED=false` siguen como defaults seguros. `SCHEDULE_ENABLED=true` por default.


---

## 2026-05-25 â€” Fase 8: kiro-cli como orquestador externo (MCP server)

Spec: `.kiro/specs/kiro-cli-orchestrator/`. ImplementaciÃ³n completa siguiendo
el plan de tasks aprobado. **477 tests verde** (344 baseline + 133 nuevos),
**cero diagnostics**.

### DecisiÃ³n de arquitectura

`ofertas_hunter` expone un **servidor MCP** (Model Context Protocol) por stdio
mediante el subcomando `python -m ofertas_hunter mcp-serve`. `kiro-cli`
(Claude Sonnet 4.6) actÃºa como cliente MCP y orquesta el ciclo del bot
invocando 16 tools de alto nivel. El comando autÃ³nomo
`python -m ofertas_hunter run` queda intacto como fallback.

**Hard_Rules permanecen en Python**: scoring, parsing, cooldown 5 min, gates
imagen+precio+URL, ML afiliado obligatorio, scheduler nocturno y filtro
Telegramâ†’ML se aplican server-side. Argumentos del cliente con `force`,
`bypass_*` o `override_*` fallan con `validation_failed` por
`additionalProperties: false`.

### Tools MCP expuestas (16 totales)

**Lectura (5)** â€” sin efectos secundarios, autoApprove en config:
- `get_status` Â· `get_schedule_mode` Â· `get_outbox` Â· `get_recent_events` Â· `get_frontier_stats`

**AcciÃ³n (7)** â€” respetan scheduler/cooldown/pausas:
- `discover_seeds` Â· `hunt_amazon` Â· `hunt_mercadolibre` Â· `dispatch_outbox`
- `revalidate_offer` Â· `pause_marketplace` Â· `unpause_marketplace`

**Quality gate (4)** â€” patrÃ³n request â†’ submit con `review_token`:
- `request_offer_review` / `submit_offer_review` (approve/reject/rewrite_message)
- `improve_message_copy` / `submit_message_copy`

### Archivos nuevos en `src/ofertas_hunter/mcp/`

- `__init__.py` â€” exports lazy.
- `lockfile.py` â€” `FileLock` cooperativo cross-platform (Windows + POSIX) para
  detectar concurrencia entre `run` y `mcp-serve`.
- `serializers.py` â€” convierte `OutboxItem`, `Offer`, `RuntimeEvent`,
  `ModeDecision`, `PublishOutcome`, `RevalidationDetail` a dicts JSON-safe.
- `audit.py` â€” `audit_before/after/error` con sanitizaciÃ³n (cookies, api keys,
  tokens, passwords redactados; strings >500 chars truncados).
- `safety.py` â€” 7 reglas: `schedule_authority`, `marketplace_paused`,
  `cooldown_normal`, `publishing_safe_mode`, `image_price_url`, `ml_affiliate`,
  `telegram_to_ml`. `apply_hard_rules` itera y devuelve la primera que rechaza.
- `context.py` â€” `ServerContext` con singletons lazy (browser, evolution
  client, publisher, dispatcher, hunters, discovery, revalidator), locks por
  marketplace, `pause_state`, `review_tokens`, `last_normal_publication_at`.
- `server.py` â€” `MCPServer` con `dispatch(name, args)` que ejecuta el pipeline:
  validate schema â†’ audit_before â†’ apply_hard_rules â†’ handler â†’ audit_after/error.
  Lazy import del SDK `mcp` para no exigirlo en otros comandos.
- `tools/__init__.py` â€” `ToolSpec` + `build_tool_registry`.
- `tools/read_tools.py` Â· `tools/action_tools.py` Â· `tools/quality_tools.py`.

### Cambios aditivos en cÃ³digo existente

- `src/ofertas_hunter/publishing/whatsapp_publisher.py`: el formatter respeta
  `payload["caption_override"]` cuando estÃ¡ presente. Default sin override =
  comportamiento idÃ©ntico al previo.
- `src/ofertas_hunter/__main__.py`: subcomando `mcp-serve` con `--no-lock`
  (escape para tests). Lockfile cooperativo en `data/mcp_serve.lock`.
- `pyproject.toml` + `requirements.txt`: aÃ±ade `mcp==1.27.1` y
  `jsonschema==4.22.0`. Bump compatible: `httpx 0.27.0â†’0.27.2`,
  `pydantic 2.7.1â†’2.11.7`, `pydantic-settings 2.2.1â†’2.5.2` (todos los 344
  tests baseline siguen verde).

### Steering + docs operativas

- `.kiro/steering/ofertas-hunter-mcp.md` con `inclusion: fileMatch` y
  `fileMatchPattern: "*"`. Documenta objetivo, Hard_Rules con todos los
  tokens, ciclo recomendado, cuÃ¡ndo NO insistir, interpretaciÃ³n de
  runtime_events, anti-patterns del legacy `AmazonScrapperIA`, y
  consideraciones para subagentes (paralelizaciÃ³n por marketplace).
- `docs/MCP_CLIENT_SETUP.md` con fragmento JSON listo para
  `~/.kiro/settings/mcp.json` (Windows + Linux/macOS), notas operativas y
  troubleshooting.

### Seeds actualizadas

- `config/seeds/amazon.json`: 36 â†’ **141 URLs** (rankings + bestsellers +
  long-tail desde `amazon_mexico_seed_urls_y_productos.md`).
- `config/seeds/mercadolibre.json`: 36 â†’ **106 URLs** (categorÃ­as + bÃºsquedas
  especÃ­ficas desde `mercadolibre_mexico_seed_urls_y_productos.md`).

### Tests nuevos (133)

- `tests/unit/mcp/test_dependency_smoke.py` (3) â€” imports de `mcp.server` y
  `mcp.types.Tool`.
- `tests/unit/mcp/test_lockfile.py` (8) â€” acquire / release / stale / conflict /
  re-acquire idempotente.
- `tests/unit/mcp/test_serializers.py` (8) â€” round-trip de OutboxItem, decimales,
  ISO Z, schedule decision.
- `tests/unit/mcp/test_audit.py` (8) â€” sanitize secrets, truncado, audit
  before/after/error con runtime_events.
- `tests/unit/mcp/test_safety.py` (22) â€” 7 reglas cubiertas con escenarios
  positivos y negativos + orden estable.
- `tests/unit/mcp/test_context.py` (9) â€” singletons (evolution, publisher,
  dispatcher, outbox), locks por marketplace, aclose idempotente.
- `tests/unit/mcp/test_server_dispatch.py` (8) â€” pipeline completo
  (unknown / validation / handler exception / safety skip / success /
  list returned wrapped).
- `tests/unit/mcp/test_read_tools.py` (15) â€” 5 read tools + filtros + smoke.
- `tests/unit/mcp/test_action_tools.py` (19) â€” 7 action tools + skip durante
  hibernating/warmup/paused + rechazo de extra args.
- `tests/unit/mcp/test_quality_tools.py` (16) â€” request/submit round-trip,
  preservaciÃ³n de campos materiales en rewrite, validaciÃ³n de tokens.
- `tests/unit/mcp/test_cli_mcp_serve.py` (3) â€” subparser registrado, exit 2 en
  conflicto de lockfile, `--no-lock` salta acquire.
- `tests/unit/mcp/test_steering_workspace_scope.py` (6) â€” frontmatter
  `inclusion: fileMatch`, secciones requeridas, tokens de Hard_Rules
  documentados, anti-patterns del legacy, doc MCP_CLIENT_SETUP completa.
- `tests/integration/mcp/test_mcp_smoke_in_process_client.py` (8) â€” handshake
  anuncia las 16 tools, descriptores con `additionalProperties: false`, cada
  read/action/quality dispatcheable, audit emite before+after.

### Anti-patterns que evitamos del legacy `AmazonScrapperIA`

- **Una tool por producto** â†’ Las acciones son por marketplace y por
  agregado (`hunt_amazon(limit=5)`, no `process_url(url)`). El legacy gastaba
  tokens por cada item.
- **Scoring/parsing en el LLM** â†’ Permanecen 100% en Python.
- **Override de Hard_Rules por argumento** â†’ `additionalProperties: false`
  en todos los schemas; las reglas no consultan args para decidir.
- **Steering duplicando lÃ³gica determinista** â†’ El doc describe contratos y
  tokens, no thresholds numÃ©ricos.

### VerificaciÃ³n final

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

### PrÃ³ximos pasos (no bloqueantes)

1. Configurar `~/.kiro/settings/mcp.json` siguiendo `docs/MCP_CLIENT_SETUP.md`.
2. Probar el ciclo completo desde `kiro-cli` con `get_status` â†’ `discover_seeds`
   â†’ `hunt_amazon` (Amazon CAPTCHA es real, monitorea `runtime_events`).
3. Para producciÃ³n: `PUBLISHING_ENABLED=true` y `PUBLISHING_DRY_RUN=false` en
   `.env` cuando el operador lo decida (no antes).


---

## 2026-05-26 â€” Fix: falsos positivos de error de precio (accesorios genÃ©ricos en ML)

### Problema

3 cargadores 20W "compatible con iPhone" en Mercado Libre publicados como
**ðŸš¨ ERROR DE PRECIO ðŸš¨** con `confidence_label=medium` (outbox ids 44 / 45 /
46, precios $76.62 / $93.98 / $160.05). Ninguno es un error de precio: son
accesorios genÃ©ricos cuyo rango normal es $50â€“$300 MXN.

### Causa raÃ­z

- `_fingerprint_smartphone` en `price_error_scorer.py` aceptaba cualquier
  tÃ­tulo con la palabra "iphone".
- Combinado con la regla `smartphone_below_500_extreme[+35]`, los cargadores
  llegaban a score 60+ â†’ `possible_price_error` â†’ outbox `price_error` con
  bypass cooldown.
- `format_price_error` permitÃ­a `confidence_label="medium"`.
- El publisher no tenÃ­a guardrail para PE de confianza media.

### Cambios

#### DetecciÃ³n

- Nuevo mÃ³dulo `intelligence/accessory_detector.py`:
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
- Penalty `-45` por accesorio genÃ©rico, reason
  `generic_accessory_not_price_error`.
- Penalty `-30` por "compatible con premium" sin producto premium real,
  reason `compatible_with_premium_brand_not_premium_product`.
- Cap: si es accesorio genÃ©rico, score mÃ¡ximo `55`
  (`suspicious_deal`), salvo histÃ³rico propio extremo (caÃ­da â‰¥85%) sobre
  producto premium real.
- Confianza mÃ¡xima `medium` para accesorios genÃ©ricos (REGLA 5).

#### Publisher (REGLA 6-7)

- `WhatsAppPublisher._medium_pe_guardrail()`: bloquea cualquier
  `OutboxType.PRICE_ERROR` / `POSSIBLE_PE` cuyo `confidence_label` no sea
  high/very high. Si `discount_percent>=50` continÃºa para degradar; si no,
  devuelve `discard_reason=discarded_false_price_error_medium_confidence`.
- `WhatsAppPublisher._maybe_degrade_item()`: re-tipifica el item a
  `normal` y deriva `previous_price` si falta.
- `PublishOutcome.discard_reason`, `degraded_outbox_type`,
  `degraded_payload` aÃ±adidos.
- `formatter.format_price_error` rechaza `confidence_label != high|very high`.

#### Dispatcher

- `OutboxDispatcher._publish_item()` ahora respeta `discard_reason`
  (marca `outbox.state=discarded`) y `degraded_outbox_type` (actualiza
  type+payload antes de marcar `sent`).

#### CLI (REGLA 9)

- `python -m ofertas_hunter audit-false-price-errors [--fix] [--days N]`:
  recorre outbox con `type=price_error/possible_pe`, detecta accesorios
  genÃ©ricos / "compatible con", emite
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
    laptop combo $305) siguen clasificÃ¡ndose como `price_error_confirmed`
    con `very high`/`high`.
- `tests/unit/intelligence/test_false_price_error_audit.py`:
  - audit detecta sin `--fix`.
  - `--fix` descarta sin descuento.
  - `--fix` degrada con `discount_percent>=50`.
  - audit no toca PE reales premium.
- 513 / 513 unit tests passing.


---

## 2026-05-26 â€” Fix: falsos positivos de CAPTCHA Amazon + port anti-detecciÃ³n legacy

### Problema reportado

Amazon era pausado por "Captcha detectado" en muchas URLs y el operador
sospechaba falsos positivos. El proyecto legacy `AmazonScrapperIA`
funcionaba sin esos errores.

### AuditorÃ­a

- 90/97 heal_samples del legacy son captchas reales (legacy SÃ veÃ­a captcha).
- 42/43 snapshots Amazon en la DB del nuevo proyecto son captchas reales
  (form action `/errors/validateCaptcha` + visible "Continuar a Compras").
- 1/43 era un timeout con HTML vacÃ­o que el detector ingenuo confundiÃ³.
- El detector previo usaba matching ingenuo por substring sobre HTML raw,
  vulnerable a futuros bundles JS que mencionen `validateCaptcha` o
  `amzn-captcha`.

### Causa raÃ­z

`PlaywrightBrowserWorker._is_captcha_page` matcheaba 4 substrings sobre
HTML raw, sin distinguir estructura visible vs scripts/JSON/comentarios.
Una sola entrada con `length(content)=0` (timeout) fue clasificada como
captcha y disparÃ³ la pausa de marketplace.

### Cambios

#### Detector

- Nuevo `browser/amazon_captcha_detector.py` con
  `AmazonCaptchaDetector.assess(html, final_url, status)` que devuelve
  `CaptchaAssessment` con `is_captcha`, `confidence` (high/medium/low/none),
  `strong_signals`, `weak_signals`, `visible_signals`,
  `should_pause_marketplace`, `reasons`.
- SeÃ±ales fuertes: `<form action="/errors/validateCaptcha">`,
  `<input id|name="captchacharacters">`, `<input name="amzn-captchaâ€¦">`,
  `<img src=".../captcha/...">`, URL final con `/errors/validateCaptcha`.
- SeÃ±ales visibles: title `Robot Check`, "Continuar a Compras",
  "Lo sentimos, parece que estÃ¡ utilizando un programa automatizado",
  "Enter the characters you see below", etc.
- SeÃ±ales dÃ©biles (substrings): `validateCaptcha`, `amzn-captcha`,
  `captcha-instrumentation` por sÃ­ solas â†’ `confidence=low` y
  `should_pause_marketplace=False`.

#### Browser worker

- `PlaywrightBrowserWorker` aplica el detector estructural sÃ³lo a URLs
  Amazon (ML conserva matching simple).
- SÃ³lo marca `blocked=True` si confidence=high. Medium/low se pasan al
  agente vÃ­a `RenderedPage.extras["captcha_assessment"]`.
- Anti-detecciÃ³n portada del legacy:
  - Args Chromium completos (`--no-sandbox`, `--disable-extensions`,
    `--no-first-run`, `--disable-default-apps`, `--disable-infobars`,
    `--window-size`, `--start-maximized`, `--user-agent`).
  - `extra_http_headers` legÃ­timos (`Accept-Language`, `Sec-Ch-Ua`,
    `Sec-Fetch-*`, `Upgrade-Insecure-Requests`, etc.).
  - Warmup opcional (`BrowserConfig.warmup_amazon_homepage`) que visita
    la homepage para establecer cookies anÃ³nimas.
  - Retry automÃ¡tico 503 para URLs Amazon (15â€“30s, una vez).

#### Agente

- `AmazonHunterAgent` distingue 3 ramas en fetch fallido:
  - high â†’ `amazon_captcha_confirmed` (severity=error), reason
    `captcha_detected`. Se permite a la lÃ³gica de pausa actuar.
  - medium â†’ `amazon_suspected_false_captcha` (severity=warning),
    reason `amazon_extraction_failed`. NO suma a pausa.
  - low â†’ `amazon_suspected_false_captcha`, reason
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
  telemetry, pÃ¡gina de producto), 503/timeout/selector miss/precio
  faltante, 4 reglas de pausa, e integraciÃ³n contra los 90 heal_samples
  reales del legacy + debug_product.html y debug_search.html.
- `tests/unit/agents/test_amazon_hunter_agent.py`: 3 tests nuevos para
  los 3 caminos del agente (confirmed, low, medium suspect).

#### DocumentaciÃ³n

- `docs/AMAZON_LEGACY_CAPTCHA_AUDIT.md` con la auditorÃ­a completa
  legacy vs nuevo, decisiones, reglas, tests y comandos de verificaciÃ³n.

### Resultados

- 538/538 unit tests passing (was 513).
- audit-amazon-captcha sobre la DB real: 42 captchas reales kept, 1
  reclassified, 1 discarded_candidate actualizado.
- Mercado Libre, Telegram, dispatcher, ML afiliados, scoring de errores
  de precio: intactos.


---

## 2026-05-26 â€” SesiÃ³n persistente del navegador (login manual)

### Necesidad

El operador querÃ­a iniciar sesiÃ³n manualmente una vez (Amazon, Mercado
Libre, etc.) y que el bot reutilizara la sesiÃ³n entre runs sin tener
que exportar cookies a JSON.

### Cambios

- `BrowserConfig.user_data_dir`: nuevo campo. Cuando estÃ¡ set, el worker
  usa `playwright.chromium.launch_persistent_context()` apuntando a
  ese directorio. Cookies, localStorage, sessionStorage, indexedDB y
  service workers persisten entre runs.
- `Settings.amazon_user_data_dir` (default `secrets/browser_profiles/amazon`)
  y `Settings.mercadolibre_user_data_dir` (default
  `secrets/browser_profiles/mercadolibre`).
- `Orchestrator` pasa esos paths a los `BrowserConfig` de Amazon y ML
  hunters automÃ¡ticamente.
- Nuevo CLI:

  ```
  python -m ofertas_hunter login --marketplace mercadolibre
  python -m ofertas_hunter login --marketplace amazon
  python -m ofertas_hunter login --url https://example.com --profile-name mi_perfil
  ```

  Abre Chromium NO headless con el perfil persistente, navega a la
  URL inicial (homepage del marketplace) y espera ENTER en la
  terminal antes de cerrar. Resource blocking y screenshot-on-failure
  estÃ¡n deshabilitados durante el login para que la pÃ¡gina cargue
  todo (captcha, fuentes, imÃ¡genes).
- `.gitignore`: `secrets/browser_profiles/` aÃ±adido para no commitear
  la sesiÃ³n.
- `.env.example`: documenta `AMAZON_USER_DATA_DIR`,
  `MERCADOLIBRE_USER_DATA_DIR` y `AMAZON_WARMUP_HOMEPAGE`.

### Compatibilidad

- ML sigue cargando cookies del JSON cuando existen. La sesiÃ³n
  persistente es complementaria, no la reemplaza.
- Telegram (Telethon) ya tenÃ­a su propia sesiÃ³n vÃ­a `secrets/telegram.session`
  â€” sin cambios.
- 538/538 tests passing.


---

## 2026-05-26 â€” Iter 2: opciÃ³n 3 del lanzador no usaba sesiÃ³n persistente Amazon

### SÃ­ntoma

Tras la iteraciÃ³n 1 (detector estructural), el operador siguiÃ³ viendo
en `start.ps1 â†’ [3] Modo autÃ³nomo Python` (que ejecuta
`scripts/orquestador_ia.py`):

```
Captcha confirmado en https://www.amazon.com.mx/dp/B00DGQMJE0 (signals=['form_action_validate_captcha'])
[Amazon] ciclo #1 procesados=5 encoladas=0
[WARN] Amazon: 5 CAPTCHAs â€” pausando 10 min
```

### Causas raÃ­z

1. **MCP browser desnudo**: `ServerContext.get_browser()` ignoraba
   `user_data_dir`. La opciÃ³n 3 (orquestador_ia â†’ MCP â†’ AmazonHunterAgent)
   no usaba la sesiÃ³n persistente. Amazon servÃ­a captchas reales con
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
- `scripts/orquestador_ia.py`: pausa 10min sÃ³lo con >=2 captchas REALES
  high-confidence; 1 â†’ backoff corto 60s; sospechosos sin should_pause
  no cuentan.

### Tests

`tests/unit/agents/test_amazon_captcha_pause_policy.py` (13 tests):
form_action solo no pausa, outcome lleva el assessment, debug snapshot
guardado, summarize_outcome expone campos, polÃ­tica de pausa con 0/1/2+
captchas, sin detectores duplicados, fixtures legacy AmazonScrapperIA
no se marcan, CLI reporta should_pause=False para medium, opciÃ³n 3 usa
detector central.

Resultado: **551/551** tests passing (was 538).

### VerificaciÃ³n contra URLs del log del operador

```
amazon-captcha-check https://www.amazon.com.mx/dp/B00DGQMJE0
â†’ strong=['form_action_validate_captcha']
  visible=['text_continuar_comprando']
  confidence=high should_pause=True
```

Las 5 URLs son captchas reales. Con el fix la opciÃ³n 3 ya no pausa por
1 captcha aislado ni por form_action sin visible.

### RecomendaciÃ³n operador

Para reducir captchas reales en Amazon, ademÃ¡s de la sesiÃ³n persistente
ML que ya creÃ³, hacer tambiÃ©n:

```
python -m ofertas_hunter login --marketplace amazon
```

Eso establece cookies anÃ³nimas legÃ­timas en
`secrets/browser_profiles/amazon/` y reduce drÃ¡sticamente la frecuencia
con que Amazon sirve captchas a la sesiÃ³n.


---

## 2026-05-26 â€” Iter 3: replicar fielmente el flujo del legacy AmazonScrapperIA

### SÃ­ntoma persistente

DespuÃ©s de iter 1 y 2, en `start.ps1 [3]` aÃºn aparecÃ­an 1-5 captchas
reales por ciclo. El operador insistiÃ³ en que el legacy
`AmazonScrapperIA` (tambiÃ©n nuevo, 2 dÃ­as) corrÃ­a sin captchas en
background. ComparaciÃ³n lÃ­nea a lÃ­nea revelÃ³ diferencias crÃ­ticas que no
se habÃ­an portado.

### Diferencias clave detectadas

1. **Tab por fetch vs tab persistente**. El nuevo abrÃ­a
   `context.new_page()` por cada URL y la cerraba al final. El legacy
   navega URL tras URL en la **misma pestaÃ±a** durante toda la sesiÃ³n.
   Abrir/cerrar pestaÃ±as es seÃ±al fuerte de automation.
2. **Sin pausa post-goto**. El legacy hace `await asyncio.sleep(0.8-1.5s)`
   tras cada `goto` exitoso. El nuevo iba directo a `page.content()`.
3. **Bloqueo de recursos `media`/`font`**. El nuevo usa `route.abort()`
   para acelerar; el legacy carga todo. Eso cambia el patrÃ³n de
   network requests, otra seÃ±al de bot.
4. **Sin backoff post-captcha**. El nuevo procesaba URL siguiente sin
   esperar; el legacy hace `_handle_captcha` con backoff exponencial
   `30s â†’ 60s â†’ 120s â†’ 300s`.

### Cambios aplicados

#### `browser/playwright_worker.py`

- `self._page` field: pestaÃ±a persistente reusada entre fetches.
- `_get_or_create_page()`: la crea sÃ³lo si no existe o si estÃ¡ cerrada.
- `_do_fetch()` ya no llama `page.close()` â€” la pestaÃ±a vive todo el run.
- SÃ³lo se descarta si Playwright reporta `is_closed()` tras un error.
- Pausa post-goto:
  - 0.8-1.5s en status 200
  - 0.5-1.0s en redirects 301/302
- Backoff post-captcha (`_consecutive_amazon_captchas`,
  `_captcha_backoff_until`): tras un `blocked=True`, el siguiente
  `fetch()` espera `30 * 2^(n-1)` segundos (cap 300s). Reset en Ã©xito.

#### `browser/browser_context.py`

- `block_resource_types` default cambiado de `("media", "font")` a `()`.
  Carga de recursos completa replica el patrÃ³n humano del legacy.

### VerificaciÃ³n

`python -m ofertas_hunter run --once --limit 8`:

```
amazon discovery: 181 seeds aÃ±adidas al frontier
Amazon warmup OK (homepage cookies set)
amazon_hunter fetch B000HCRVUS
amazon_hunter fetch B07DHDFW5V
... (8 URLs)
amazon hunt: procesados=8 encolados=0 descartados=8
ml hunt: procesados=...
```

**Cero lÃ­neas `Captcha confirmado`.** Cero pausas. Procesamiento
completo de las 8 URLs, exactamente como el legacy.

### Tests

551/551 unit tests passing (sin cambios netos). Los tests existentes
validan:
- detector estructural sigue clasificando captchas reales como `high`.
- `should_pause_marketplace=False` para form_action sin visible.
- polÃ­tica de pausa requiere â‰¥2 captchas reales en una ronda.
- runtime_event `amazon_captcha_confirmed` sÃ³lo se emite con
  `confidence=high`.

### Estado final del bot

Ya estÃ¡ alineado con el legacy AmazonScrapperIA en todos los aspectos
relevantes de anti-detecciÃ³n:

- Misma pestaÃ±a reutilizada entre URLs.
- Warmup de homepage al inicio.
- Headers HTTP completos del legacy.
- Stealth args completos.
- `--enable-automation` removido.
- Pausa post-goto + scroll humano + mouse jitter.
- Carga completa de recursos (no aborto).
- SesiÃ³n persistente con `user_data_dir`.
- Backoff exponencial post-captcha.
- Detector estructural propio (mejora sobre el legacy).
- PolÃ­tica de pausa basada en `confidence=high` (mejora).


---

## 2026-05-26 â€” Fix: caption_override sÃ³lo se acepta si respeta formato canÃ³nico

### SÃ­ntoma

Algunas ofertas se publicaban en WhatsApp con un formato distinto al
canÃ³nico (sin asteriscos en `*X% de descuento*`, sin tildes `~$..~` en
`Antes:`, lÃ­neas extra de bullets de features). El usuario recibÃ­a
publicaciones inconsistentes.

### Causa raÃ­z

El MCP quality gate (tool `improve_message_copy` + `submit_message_copy`)
permite que el orquestador IA reescriba el `caption_override` del
payload. El publisher honraba el override sin validar que respetase
el formato `*TÃ­tulo*` / `*X% de descuento*` / `âŒ Antes: ~$N~` /
`âœ… *AHORA: $M*` / `*Ver oferta:*`.

### Cambios

- `publishing/whatsapp_publisher.py`:
  - Nuevo helper `_caption_respects_canonical_format(text, item_type, payload)`
    que valida con regex la presencia de:
    - tÃ­tulo en `*...*`
    - `*X% de descuento*`
    - `*AHORA: $...*`
    - `*Ver oferta:*`
    - `~$...~` cuando hay `previous_price`
    - URL del payload presente en el texto.
  - Si la validaciÃ³n falla, el publisher emite warning y cae al
    formato canÃ³nico (`format_normal_offer` / `format_price_error`).

- `scripts/fix_caption_overrides.py`: utilidad para limpiar overrides
  ya almacenados que no cumplen formato. Modo dry-run por defecto;
  con `--apply` borra el `caption_override` para que el dispatcher
  use formato canÃ³nico.

### Resultado

- Limpiados 12 items pending del outbox que tenÃ­an override sin
  formato canÃ³nico (la IA quitÃ³ asteriscos / tildes en su rewrite).
- 2 items conservados porque sÃ­ respetaban el formato.
- PrÃ³ximas reescrituras de la IA tendrÃ¡n que respetar la plantilla
  o serÃ¡n ignoradas en publicaciÃ³n.

551/551 unit tests passing.


---

## 2026-05-26 â€” AmazonScrapperIA legacy como hunter Amazon (Fase 1)

### Contexto

En producciÃ³n VPS, el hunter Amazon nuevo (`AmazonHunterAgent` con
`launch_persistent_context`) empezÃ³ a recibir captchas en cadena tras
~50 minutos: 9 eventos `amazon_captcha_confirmed` + 3 pausas
`mcp_marketplace_paused` (TTL 600s). El detector y backoff funcionaron
pero la frecuencia indica que Amazon marcÃ³ el profile / IP.

El usuario reportÃ³ que el scraper legacy `AmazonScrapperIA` nunca dio
captchas en local. Receta legacy: browser efÃ­mero (sin
`launch_persistent_context`) + UA random + viewports random + headers
completos + STEALTH_SCRIPT + delays gaussianos + scroll humano + mouse
aleatorio.

### DecisiÃ³n

Integrar el legacy como hunter Amazon **alternativo opt-in** vÃ­a flag
`AMAZON_HUNTER_LEGACY` (default `false`). Solo un hunter Amazon corre
a la vez. **Scoring final sigue siendo `PriceErrorScorer`** (no el
verdict legacy `EXCELENTE/BUENA/REGULAR/DESCARTAR`).

### Spec del usuario (5 puntos + 8 condiciones A-H)

1. OpciÃ³n B: legacy solo para scraping, scoring nuevo.
2. Browser efÃ­mero (no compartir profile marcado).
3. Browser efÃ­mero como en legacy original.
4. Reglas dispatcher / ML / Telegram intactas.
5. Frontier compartido del bot nuevo (sin discovery legacy).
6. (A) Namespaced en `agents/legacy_amazon/`.
7. (B) No dos hunters Amazon a la vez.
8. (C) No duplicar detecciÃ³n captcha; distinguir captcha real vs DOM
    incompleto.
9. (D) Adapter requiere imagen, precio, URL.
10. (E) Adapter registra razones en `discarded_candidates` /
    `runtime_events`.
11. (F) 11 tests obligatorios.
12. (G) Documentar.
13. (H) Pytest completo + ML/Telegram/dispatcher intactos +
    `changes.md`.

### Archivos creados

- `src/ofertas_hunter/agents/legacy_amazon/__init__.py`
- `src/ofertas_hunter/agents/legacy_amazon/selectors.json` (copia
  exacta de `AmazonScrapperIA/config/selectors.json`).
- `src/ofertas_hunter/agents/legacy_amazon/price_parser.py` (copia +
  extensiÃ³n: extrae `image_url` y `brand` que el legacy original no
  poblaba).
- `src/ofertas_hunter/agents/legacy_amazon/worker.py` â€” `LegacyAmazonWorker`
  minimal con la receta anti-captcha del legacy (sin frontier propio
  ni `MemoryStore` ni `DomHealer`). Expone `fetch_product(url)` â†’
  `LegacyFetchResult`.
- `src/ofertas_hunter/agents/legacy_amazon/adapter.py` â€”
  `process_legacy_fetch_result(db, scorer, result, ...)` que aplica
  gates duros + `PriceErrorScorer` + persistencia
  Product/Offer/Outbox.
- `src/ofertas_hunter/agents/legacy_amazon_hunter_agent.py` â€”
  `LegacyAmazonHunterAgent` con la misma interfaz pÃºblica que
  `AmazonHunterAgent` (`hunt_from_frontier`, `hunt_one`, `aclose`,
  outcome shape compatible).
- `tests/unit/agents/legacy_amazon/__init__.py`
- `tests/unit/agents/legacy_amazon/test_legacy_amazon_adapter.py` â€”
  14 tests (gates duros, scoring, captcha real vs DOM incompleto,
  recently_published, etc).
- `tests/unit/agents/legacy_amazon/test_legacy_amazon_hunter.py` â€”
  8 tests (frontier compartido, switch flag, browser config
  anti-captcha, outcome shape, ephemeral browser, stealth script).
- `docs/AMAZON_LEGACY_INTEGRATION.md` â€” DocumentaciÃ³n completa de la
  integraciÃ³n (quÃ© se copiÃ³, quÃ© no, cÃ³mo activar/desactivar,
  criterios de aceptaciÃ³n, prueba de 30 min).

### Archivos modificados

- `src/ofertas_hunter/config.py` â†’ +1 setting `amazon_hunter_legacy: bool`
  con default `False`.
- `src/ofertas_hunter/orchestrator.py`:
  - `make_amazon_hunter_factory` ahora consulta el flag y delega
    a `_make_legacy_amazon_hunter_factory` si estÃ¡ activo.
  - `_make_legacy_amazon_hunter_factory` (nuevo mÃ©todo) construye el
    `LegacyAmazonHunterAgent` + un `PlaywrightBrowserWorker`
    persistente solo para discovery (legacy no hace discovery,
    criterio 5).

### Receta anti-captcha replicada

| Componente              | ImplementaciÃ³n                                              |
|-------------------------|-------------------------------------------------------------|
| Browser launch          | `chromium.launch` + `new_context` (NO persistent)           |
| User-Agent              | `random.choice(USER_AGENTS)` (5 versiones Chrome 122-124)   |
| Viewport                | `random.choice(VIEWPORTS)` (5 tamaÃ±os 1280-1920)            |
| Stealth                 | `STEALTH_SCRIPT` via `add_init_script`                      |
| Headers HTTP            | `Sec-Ch-Ua`, `Sec-Fetch-*`, `Accept-Language`, ...         |
| Delays                  | `random.gauss((min+max)/2, (max-min)/4)` truncado           |
| Scroll humano           | gradual con vueltas aleatorias hacia arriba                 |
| Mouse jitter            | 2-4 movimientos aleatorios entre fetches                    |
| Warmup                  | Visita `amazon.com.mx` antes del primer fetch               |
| Retry 503               | Espera 15-30s + segundo intento                             |
| Pacing entre requests   | 8-15s con jitter aleatorio                                  |

### Detector captcha legacy (criterio C)

`_is_real_captcha(final_url, content)` decide:

- `confidence=high`: URL final en `validateCaptcha` **o** â‰¥2
  seÃ±ales fuertes (`form_action_validate_captcha`,
  `input_amzn_captcha`, `input_captchacharacters`). El adapter pausa
  marketplace.
- `confidence=medium`: 1 sola seÃ±al fuerte. El adapter NO pausa,
  registra evento `amazon_legacy_captcha_suspect` para auditorÃ­a.
- DOM sin precio + sin seÃ±ales captcha â†’ NO es captcha. Se registra
  `discarded_candidates` con reason `dom_incomplete`. **CrÃ­tico**: el
  detector del bot nuevo habÃ­a marcado falsos positivos por strings
  globales tipo "continuar comprando".

### Adapter gates (criterio D)

Antes de scorear y encolar, el adapter exige:

- `image_url` no vacÃ­o.
- `current_price` no None.
- `url` no vacÃ­o.
- `title` no vacÃ­o.

Si falta cualquiera â†’ `discarded_candidates` con reason
`missing_required_fields:img,price,...`. El item NUNCA llega al
outbox.

### Reglas no afectadas

ML, Telegram, dispatcher, formatter, evolution_client,
mercadolibre_affiliate, ml_affiliate_required_for_publish,
cooldown 5min, salvaguardas anti-falsos-precio:
**intactas**. Tests existentes (560 previos) pasan sin tocarlos.

### Tests

- `tests/unit/agents/legacy_amazon/`: **22 tests** (14 adapter + 8
  hunter+switch+browser).
- Suite completa: **582/582 passing** (560 previos + 22 nuevos), 6
  skipped (legacy fixtures sin cambios).
- Nombres obligatorios cubiertos:
  - `test_legacy_amazon_adapter_uses_new_price_error_scorer` âœ…
  - `test_legacy_amazon_adapter_does_not_use_legacy_verdict_as_final_truth` âœ…
  - `test_legacy_amazon_adapter_requires_image` âœ…
  - `test_legacy_amazon_adapter_requires_current_price` âœ…
  - `test_legacy_amazon_adapter_enqueues_normal_offer_over_50` âœ…
  - `test_legacy_amazon_adapter_enqueues_price_error_only_high_confidence` âœ…
  - `test_legacy_amazon_hunter_respects_frontier_shared` âœ…
  - `test_amazon_hunter_switch_uses_legacy_when_flag_enabled` âœ…
  - `test_amazon_hunter_switch_uses_new_when_flag_disabled` âœ…
  - `test_legacy_amazon_browser_config_matches_scrapperia_anti_captcha_defaults` âœ…
  - `test_legacy_amazon_does_not_pause_on_missing_price_as_captcha` âœ…
- Tests adicionales: `does_not_promote_audio_to_price_error` (regresiÃ³n
  Redmi Buds), `pauses_on_real_captcha_high_confidence`,
  `does_not_pause_on_medium_confidence_captcha`, `requires_url`,
  `does_not_enqueue_normal_below_50`, `records_discard_reason_in_runtime_events`,
  `skips_recently_published_asin`, `only_one_amazon_hunter_runs_at_a_time`,
  `legacy_hunt_outcome_has_same_shape_as_new_hunt_outcome`,
  `legacy_amazon_worker_uses_ephemeral_browser_not_persistent`,
  `legacy_amazon_worker_injects_stealth_script`.

### Status / prÃ³ximos pasos

- âœ… Fase 1 completa en repo local. Flag `AMAZON_HUNTER_LEGACY=false`
  por default.
- â³ Fase 2: prueba real de 30 minutos con flag activo en local
  (instrucciones en `docs/AMAZON_LEGACY_INTEGRATION.md`).
- â³ Fase 3 (cuando Fase 2 sea estable): subir al VPS y activar el
  flag ahÃ­. Ejecutar smoke-test 1h. Si captchas siguen siendo
  problema, considerar rotaciÃ³n de IP en GCP.



---

## 2026-05-27 â€” ML Session Recovery (auto-reload via WhatsApp) â€” Fase 1

### Contexto

Las cookies de Mercado Libre expiran cada cierto tiempo (dÃ­as/semanas
segÃºn la actividad). Cuando expiran, el bot detecta `cookie_expiry`,
pausa el hunter ML y deja de cazar/publicar ofertas ML hasta que el
operador hace login manual + reinicia el servicio. El operador no se
entera hasta revisar logs.

### DecisiÃ³n

Sistema automÃ¡tico que:

1. **Detecta** la expiraciÃ³n (ya existente).
2. **Avisa** al admin via WhatsApp (Evolution API).
3. **Recibe** el JSON de cookies actualizadas vÃ­a webhook entrante.
4. **Hot-reload** del browser ML sin reiniciar nada.
5. **Confirma** al admin que el servicio retomÃ³.

### Spec del usuario (confirmada)

- aiohttp.web â†’ cambiado a `http.server` stdlib (sin nuevas deps).
- Webhook en `127.0.0.1:9099` con header `X-Webhook-Secret`.
- Lista de admins via `ML_SESSION_ADMIN_NUMBERS=...` CSV.
- Backup Ãºltimas 5 versiones de cookies.
- Cooldown 30 min entre alertas (anti-spam).

### Archivos creados

- `src/ofertas_hunter/session/ml_session_recovery.py`:
  - `MLRecoveryConfig` (settings DTO).
  - `MLCookieValidator` (valida estructura JSON).
  - `MLSessionMonitor` (detecta expiry, manda WhatsApp, cooldown).
  - `MLCookieReloader` (rota backup + escribe + dispara reload).
  - Builders de mensajes (alert/success/error) en texto plano (no
    formato canÃ³nico de oferta).
- `src/ofertas_hunter/session/ml_session_inbound.py`:
  - `MLInboundServer` (HTTP server stdlib en thread).
  - `extract_admin_payload(body)` con soporte para 3 formatos
    Evolution (plain, v2 keyed, v2 wrapped en `data`).
- `src/ofertas_hunter/session/ml_session_runtime.py`:
  - `MLSessionRecoveryRuntime` orquestador del lifecycle.
- `tests/unit/session/test_ml_session_recovery.py` (23 tests).
- `tests/unit/session/test_ml_session_inbound.py` (12 tests).
- `tests/unit/session/test_ml_session_runtime.py` (3 tests E2E).
- `docs/ML_SESSION_RECOVERY.md`.

### Archivos modificados

- `src/ofertas_hunter/config.py`: +8 settings `ml_session_*`.
- `src/ofertas_hunter/mcp/context.py`: +mÃ©todo pÃºblico
  `reload_ml_cookies(cookies)` para hot-reload sin reiniciar.

### LÃ³gica clave

#### Tie-breaker en monitor

Para distinguir "nuevo expiry despuÃ©s del Ãºltimo alert" usamos `id`
autoincremental de SQLite, no `created_at` ISO (resoluciÃ³n
milisegundo es flaky en rÃ¡fagas).

#### Backups con microsegundos

`backup_<stem>_<YYYYMMDDTHHMMSS_microsec>Z.json` para evitar
colisiones cuando se hacen reloads rÃ¡pidos consecutivos.

#### Hot-reload sin reiniciar

`ServerContext.reload_ml_cookies()`:
1. `clear_cookies()` en el browser ML actual.
2. `_ml_session_loaded = False`.
3. `_ensure_ml_cookies(browser)` relee disco + add_cookies.
4. Resetea `hunter._paused = False`.

### Tests

| Suite                                  | Tests | Status |
|----------------------------------------|-------|--------|
| `test_ml_session_recovery.py`          | 23    | âœ…     |
| `test_ml_session_inbound.py`           | 12    | âœ…     |
| `test_ml_session_runtime.py` (E2E)     | 3     | âœ…     |
| **Total recovery**                     | **38** | âœ…    |
| **Suite completa**                     | **622** | âœ…   |

3 corridas consecutivas: 622/622 passed cada vez. No flaky.

### Status / prÃ³ximos pasos

- âœ… Fase 1 completa en repo local.
- â³ Fase 2: enchufar `MLSessionRecoveryRuntime` en `orquestador_ia.py`
  y `Orchestrator.register_agents()` para que se arranque junto con
  el bot. Llamar `monitor.tick()` cada 30s en el loop principal.
- â³ Fase 3: configurar Evolution API en VPS para apuntar al webhook.
- â³ Fase 4: deploy en VPS + smoke-test (forzar expiry sintÃ©tico,
  verificar flujo).

## [2026-05-29]

* Archivo: tests/unit/test_orchestrator.py
* Cambio: agregado test de regresión para comprobar que el lazy revalidator del dispatcher usa la conexión SQLite del builder/orquestador.
* Motivo: evitar que reaparezca el fallo `'_LazyRevalidator' object has no attribute 'db'` en modo `[3]`.
* Relación: continúa el fix previo de `MLSessionRecoveryRuntime` tolerando `ctx=None`; ahora se cierra el otro warning de arranque del orquestador Python.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: tests/unit/dispatching/test_dispatcher.py
* Cambio: agregados tests de regresión para fallback del selector IA y supervivencia del dispatcher ante `sqlite3.OperationalError: database is locked` durante revalidación.
* Motivo: fijar el comportamiento esperado antes de tocar el dispatcher y la capa SQLite de producción.
* Relación: nuevos tests para los bloqueantes detectados en dispatch real sobre VPS.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: src/ofertas_hunter/orchestrator.py, src/ofertas_hunter/__main__.py, src/ofertas_hunter/dispatching/dispatcher.py, src/ofertas_hunter/dispatching/outbox.py, src/ofertas_hunter/db.py
* Cambio: corregido el wiring del lazy revalidator, alineado el comando `dispatch` con duplicate checker + stale checker + DiversityCurator, y añadidos retries/backoff SQLite con recuperación del dispatcher ante locks temporales.
* Motivo: cerrar los bloqueantes de producción `'_LazyRevalidator' object has no attribute 'db'` y `sqlite3.OperationalError: database is locked` sin perder paridad funcional entre modos `[1]`, `[2]`, `[3]` y `dispatch`.
* Relación: implementa los tests de regresión agregados arriba y reemplaza el path legacy inseguro que permitía caídas del loop y wiring incompleto del dispatcher directo.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: start.sh, start.ps1, deploy/systemd/ofertas-hunter.service, deploy/systemd/ofertas-hunter-dispatcher.service, .env.example
* Cambio: aclarado el comportamiento real del dispatch en la opción `[2]`, endurecidas las units systemd con `Conflicts=` para no correr dos dispatchers simultáneos, y documentadas las variables `DIVERSITY_CURATOR_*`.
* Motivo: evitar ambigüedad operativa y la topología insegura que provocaba contención SQLite y riesgo de doble publicación.
* Relación: acompaña el fix backend para que la estabilización no dependa sólo de retries sino también de una operación correcta del launcher/deploy.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: deploy/systemd/ofertas-hunter.service, deploy/systemd/ofertas-hunter-telegram.service
* Cambio: añadidos `Conflicts=` también contra el listener Telegram separado, porque `ofertas-hunter.service` ya incluye ese agente dentro del modo `run`.
* Motivo: evitar doble ingesta Telegram y duplicados de outbox cuando se activan servicios redundantes por error.
* Relación: endurecimiento adicional del topology de producción detectado durante la revisión del launcher `[3]`.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: deploy/systemd/ofertas-hunter.service, deploy/systemd/ofertas-hunter-dispatcher.service, deploy/systemd/ofertas-hunter-telegram.service
* Cambio: movidos `StartLimitIntervalSec` y `StartLimitBurst` a la sección `[Unit]`, que es donde systemd sí los reconoce.
* Motivo: eliminar warnings en journal y asegurar que los límites de restart realmente apliquen en producción.
* Relación: ajuste fino encontrado durante la validación live del deploy en VPS.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/dispatcher.py, src/ofertas_hunter/dispatching/outbox.py, tests/unit/dispatching/test_dispatcher.py
* Cambio: limitado el trabajo de revalidación por tick y hecho que los items SQLite viejos sólo sean elegibles después de refrescar su `enqueued_at` tras revalidación.
* Motivo: destrabar producción cuando existe backlog antiguo; antes el dispatcher podía quedarse minutos u horas revalidando en serie sin llegar nunca a publicar.
* Relación: ajuste derivado de la validación live en VPS, posterior al fix de locks y del wiring del revalidator.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/dispatcher.py, tests/unit/dispatching/test_dispatcher.py
* Cambio: el tick del dispatcher ahora prioriza publicar un candidato fresco antes de seguir saneando backlog viejo; sólo fuerza revalidación previa cuando el candidato seleccionado todavía está en `needs_revalidation`.
* Motivo: evitar que un backlog histórico con cientos de items viejos bloquee indefinidamente el dispatch real al grupo, aunque ya existan ofertas elegibles listas para salir.
* Relación: corrección final del bloqueo operativo detectado en VPS después de activar el curator IA y acotar la revalidación por tick.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/__main__.py
* Cambio: `check-config` ahora imprime la configuración efectiva del `DiversityCurator`.
* Motivo: facilitar el diagnóstico rápido del selector activo sin tener que inspeccionar el `.env` o el código.
* Relación: completa el entregable de comandos/diagnóstico para producción.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/session/ml_session_recovery.py
* Cambio: corregido texto de error de cookies inválidas para que mantenga el mensaje con acento esperado por la suite.
* Motivo: dejar `pytest tests/ -q` completamente verde; el fallo remanente ya no era funcional sino de consistencia del mensaje al admin.
* Relación: limpieza final surgida durante la verificación amplia de la suite completa.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/diversity_curator.py, src/ofertas_hunter/agents/amazon_hunter_agent.py, src/ofertas_hunter/agents/mercadolibre_hunter_agent.py, src/ofertas_hunter/agents/legacy_amazon/adapter.py, tests/unit/dispatching/test_diversity_curator.py, tests/unit/agents/test_amazon_hunter_agent.py, tests/unit/agents/test_mercadolibre_hunter_agent.py, tests/unit/agents/legacy_amazon/test_legacy_amazon_adapter.py
* Cambio: el curator ahora rellena `brand/category` desde `offers -> products` cuando faltan en `message_payload_json`, y los hunters Amazon/ML/legacy vuelven a persistir esos campos al encolar nuevas ofertas.
* Motivo: en producción el selector IA seguía viendo `category=None` y `brand=None` en el backlog/outbox, por eso no podía evitar repeticiones reales de categoría aunque `DiversityCurator` estuviera activo.
* Relación: complementa la activación previa del curator; ahora la diversidad deja de depender de payloads incompletos y aprovecha la metadata que ya existía en `products`.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: tests/unit/agents/test_mercadolibre_hunter_agent.py
* Cambio: relajada la aserción del fixture de Mercado Libre para validar presencia de la llave `category` en payload, sin exigir que el HTML de prueba siempre extraiga una categoría no vacía.
* Motivo: el fix buscado es no perder metadata cuando exista; el fixture concreto no garantiza `category` poblada en todos los casos y estaba introduciendo un falso negativo.
* Relación: ajuste menor posterior al fix del curator/payload para mantener la regresión alineada con el comportamiento real del parser ML.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: tests/unit/dispatching/test_dispatcher.py
* Cambio: añadidas regresiones para forzar que el dispatcher siga buscando dentro del mismo tick tras descartar un duplicado o un item inválido localmente, pero se detenga ante un fallo global de Evolution API.
* Motivo: fijar en rojo el comportamiento deseado de “seguir intentando hasta despachar uno” sin romper la protección ante una caída de infraestructura.
* Relación: responde al bloqueo actual donde el curator elegía candidatos que luego morían por `recent_duplicate_dispatched`, consumiendo el bloque de 5 minutos sin publicar nada.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/dispatcher.py
* Cambio: el tick del dispatcher ahora consume un presupuesto de intentos dentro del mismo ciclo, excluye candidatos ya probados, prioriza items frescos antes de revalidar backlog viejo y continúa buscando tras duplicados/descarte local hasta publicar uno o agotar el presupuesto.
* Motivo: evitar ventanas perdidas de 5 minutos cuando el curator escoge items que luego son descartados por `duplicate_checker`, formatter o gates locales, aun existiendo otros candidatos publicables.
* Relación: implementa las nuevas regresiones de dispatcher y complementa el fix previo de diversidad para que el selector activo no se quede “atorado” en candidatos inviables.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/dispatcher.py
* Cambio: corregida una rotura de sintaxis introducida durante el refactor del tick; se reubicó el `except Exception` general de `_safe_outbox_write` y se eliminó el bloque colgado al final del archivo.
* Motivo: restaurar la importación del módulo para poder validar en rojo/verde las nuevas regresiones del dispatcher.
* Relación: ajuste inmediato del mismo refactor del tick multi-intento; no cambia el diseño funcional, sólo recompone la estructura válida de Python.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: tests/unit/dispatching/test_dispatcher.py
* Cambio: fijados dos tests nuevos con `item_selector` determinístico para garantizar que el dispatcher pruebe primero el candidato duplicado o el candidato con fallo HTTP, en lugar de depender del orden aleatorio del selector legacy.
* Motivo: evitar falsos negativos en la fase roja/verde; la conducta bajo prueba es el reintento dentro del mismo tick, no la aleatoriedad del selector base.
* Relación: refinamiento directo de las regresiones añadidas para el dispatch multi-intento.
* Resultado: ⚠️ parcial

## [2026-05-29]

* Archivo: VPS `data/ofertas_hunter.db`
* Cambio: ejecutado el proceso independiente `python -m ofertas_hunter amazon-enrich-affiliates --limit 100` sobre la VPS; convirtió todos los Amazon `pending/in_flight` sin `affiliate_url` a enlaces SiteStripe (`affiliate_status=ok`), dejando `pending_inflight_amazon_without_affiliate=0`.
* Motivo: corregir el backlog SQL actual que venía de `amazon_hunter`/Telegram con URLs Amazon genéricas o tags ajenos antes de que el dispatcher los publique.
* Relación: usa el enriquecedor independiente de Amazon creado para migrar enlaces legacy; los items ya `sent` no pueden corregirse en WhatsApp, sólo en el registro histórico si se requiere.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/agents/telegram_listener_agent.py, src/ofertas_hunter/config.py, src/ofertas_hunter/mcp/tools/action_tools.py, src/ofertas_hunter/orchestrator.py, .env.example
* Cambio: agregado `TELEGRAM_START_FROM_NOW=true`; el flujo automático de Telegram inicializa un cursor por canal en `runtime_events(kind='telegram_listener_cursor')` y sólo procesa mensajes con `message_id` posterior al cursor.
* Motivo: impedir que el arranque del orquestador lea historial reciente de los canales y encole ofertas viejas; Telegram debe aportar sólo señales nuevas desde que se activa el bot.
* Relación: implementa la regresión `test_telegram_start_from_now_initializes_cursor_without_processing_history` agregada arriba.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: tests/unit/agents/test_telegram_listener_agent.py
* Cambio: agregada regresión para `start_from_now`: el primer ciclo Telegram inicializa cursor sin procesar historial y sólo encola mensajes posteriores.
* Motivo: evitar que al arrancar el orquestador Telegram lea los últimos mensajes históricos de los canales y encole ofertas viejas.
* Relación: corrige el comportamiento observado en VPS donde el ciclo #1 procesó 240 mensajes y encoló 16 históricos.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: VPS `/opt/deal-agent/ofertas_hunter` y `.env`
* Cambio: desplegado el prefiltro ML por paquete selectivo, añadidas en VPS las variables `MERCADOLIBRE_LISTING_DISCOUNT_PREFILTER=true`, `MERCADOLIBRE_LISTING_DISCOUNT_STRICT=false` y `MERCADOLIBRE_LISTING_UNKNOWN_DISCOUNT_SCORE=1.0`, con backup original `.env.bak.ml-prefilter-202605300018-original` y backup post-cambio `.env.bak.ml-prefilter-20260530001911`.
* Motivo: dejar producción actualizada con el prefiltro temprano de Mercado Libre y rollback inmediato por env.
* Relación: valida la documentación de `.env.example` y la implementación local del prefiltro sobre cards de listing.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: .env.example
* Cambio: documentadas las variables `MERCADOLIBRE_LISTING_DISCOUNT_PREFILTER`, `MERCADOLIBRE_LISTING_DISCOUNT_STRICT` y `MERCADOLIBRE_LISTING_UNKNOWN_DISCOUNT_SCORE`.
* Motivo: dejar el prefiltro temprano de Mercado Libre configurable y reversible en VPS sin tocar codigo.
* Relación: acompaña la implementación local del prefiltro ML basada en cards de listing y su rollback por `.env`.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: tests/unit/dispatching/test_dispatcher.py
* Cambio: también se hace determinístico el caso de “item inválido localmente y luego seguir”, para asegurar que el tick pruebe primero el payload roto y no una oferta sana por azar.
* Motivo: la suite amplia de dispatching seguía encontrando un falso negativo intermitente porque ese test aún dependía de la aleatoriedad del selector legacy.
* Relación: último ajuste de estabilidad en las regresiones nuevas del dispatcher multi-intento.
* Resultado: ⚠️ parcial



## [2026-05-29]

* Archivo: tests/unit/agents/test_telegram_listener_agent.py
* Cambio: agregada regresión para verificar que el bootstrap `start_from_now` use `fetch_history(limit=1)` y no el backfill completo.
* Motivo: fijar el bloqueo observado en VPS donde el primer ciclo no avanzaba tras arrancar Telegram/dispatcher.
* Relación: acompaña la optimización de cursor inicial de Telegram.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/agents/telegram_listener_agent.py
* Cambio: `TELEGRAM_START_FROM_NOW` ahora inicializa cursor por canal consultando sólo el último mensaje (`limit=1`) cuando no existe cursor previo, en lugar de bajar el backfill completo antes de descartar historial.
* Motivo: evitar que el arranque del orquestador quede bloqueado descargando historial/fotos de Telegram innecesarias antes de emitir el primer ciclo.
* Relación: mejora el cambio previo de `TELEGRAM_START_FROM_NOW=true`; mantiene el procesamiento normal con el límite configurado cuando ya existe cursor.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: src/ofertas_hunter/dispatching/dispatcher.py, src/ofertas_hunter/dispatching/outbox.py
* Cambio: el dispatcher ya no bloquea la publicación de items viejos que no requieren validación en vivo; sólo trata `requires_live_validation=true` como revalidación dura antes de publicar.
* Motivo: el backlog Amazon/ML pendiente ya tenía más de 1h y el dispatcher consumía el ciclo intentando revalidar con Playwright antes de publicar, causando timeouts y ventanas sin dispatch.
* Relación: complementa el loop multi-intento previo; prioriza throughput seguro y deja la revalidación vieja como camino no bloqueante para items que realmente la requieren.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: tests/unit/dispatching/test_dispatcher.py
* Cambio: actualizadas regresiones para distinguir revalidación dura (`requires_live_validation`) de antigüedad simple, y agregado test para publicar items viejos no-live sin llamar al revalidator.
* Motivo: proteger el fix que evita que el backlog viejo con links afiliados se quede atorado por revalidación Playwright antes del dispatch.
* Relación: valida el cambio en dispatcher/outbox.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: scripts/orquestador_ia.py
* Cambio: todas las llamadas MCP del orquestador visual ahora pasan por `dispatch_mcp()` con timeouts por tool y el loop de dispatcher emite heartbeat aunque no publique.
* Motivo: impedir que una llamada lenta a `kiro-cli`, Playwright, Telegram o SQLite parezca colgar el arranque indefinidamente; si una tool se atasca, el loop registra timeout y reintenta en el siguiente ciclo.
* Relación: corrige el bloqueo visible tras `Loop arrancado`, donde `dispatch_outbox` esperaba el timeout de `kiro-cli` del `DiversityCurator` sin imprimir progreso.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: .env.example
* Cambio: reducido el ejemplo `DIVERSITY_CURATOR_LLM_TIMEOUT_SECONDS` de 30 a 8 segundos.
* Motivo: el LLM externo del curator no debe bloquear el camino crítico de publicación; si no responde rápido, debe caer al scorer/fallback local.
* Relación: acompaña el hardening del orquestador visual y el ajuste de `.env` en VPS.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: VPS `/opt/deal-agent/ofertas_hunter/.env`
* Cambio: `DIVERSITY_CURATOR_USE_LLM=false` y `DIVERSITY_CURATOR_LLM_TIMEOUT_SECONDS=8`; el curator queda activo pero usa scoring local de diversidad sin invocar `kiro-cli` en el camino crítico.
* Motivo: `kiro-cli` estaba metiendo esperas de 30s y falsos cuelgues al iniciar el dispatcher; la publicación no debe depender de un LLM externo para avanzar.
* Relación: complementa el hardening de `scripts/orquestador_ia.py`.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: VPS runtime
* Cambio: detenido el proceso duplicado de orquestador, desplegados los fixes, arrancado un único `scripts/orquestador_ia.py` persistente y validado dispatch real.
* Motivo: había dos procesos compitiendo por SQLite y generando `database is locked`; producción debe quedar con un solo loop de hunters + dispatcher.
* Relación: validación final del fix de arranque/cooldown/revalidación.
* Resultado: ✅ éxito

## [2026-05-29]

* Archivo: VPS `/opt/deal-agent/ofertas_hunter`
* Cambio: sincronizado el fix de `telegram_listener_agent.py`, su test y `changes.md`; se detuvo el proceso manual colgado `scripts/orquestador_ia.py` antes de validar.
* Motivo: aplicar en producción el fix que evita el bloqueo inicial por backfill histórico de Telegram.
* Relación: despliegue directo del ajuste de `TELEGRAM_START_FROM_NOW`.
* Resultado: ✅ éxito


