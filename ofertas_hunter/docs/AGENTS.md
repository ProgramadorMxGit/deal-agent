# Agentes

> Cada agente es un proceso lógico (no necesariamente OS) con una responsabilidad
> única. Se sincronizan vía SQLite + asyncio queues.

## Tabla resumen

| Agente | Responsabilidad | Concurrencia | Persistencia clave |
|---|---|---|---|
| `supervisor` | coordina ciclos, límites, reinicios | 1 | `agent_runs` |
| `amazon_hunter` | explorar Amazon MX | N=1-2 | `frontier`, `visited_urls`, `products`, `price_observations` |
| `mercadolibre_hunter` | explorar Mercado Libre MX con cookies | 1 | `frontier`, `visited_urls`, `products`, `price_observations` |
| `telegram_listener` | escuchar canales | workers=3 | `telegram_messages`, `discarded_candidates` |
| `price_intelligence` | calcular descuento + score | N | `offers`, `runtime_events` |
| `dom_healer` | detectar fallos de selectores y proponer cambios | 1 | `dom_snapshots`, `selector_versions` |
| `image_resolver` | garantizar imagen en cada oferta | N=2 | `products.image_url` |
| `outbox_dispatcher` | publicar a WhatsApp respetando cooldown | 1 | `outbox`, `published_messages` |
| `memory_compressor` | compactar memoria por relevancia | 1, periódico | `memory_summaries` |
| `self_repair_agent` | aplicar patches de código si tests pasan | 1 | `self_patches` |
| `runtime_watchdog` | reiniciar agentes caídos, limitar loops destructivos | 1 | `runtime_events` |

## 1. supervisor / orchestrator

**Misión:** ser el único punto de entrada que arranca, monitorea y detiene
todos los agentes.

**Responsabilidades:**
- Cargar config y DB.
- Lanzar agentes según `--profile` (full | hunter-only | dispatcher-only |
  listener-only).
- Aplicar límites de concurrencia globales.
- Capturar SIGINT/SIGTERM y propagar shutdown limpio.
- Registrar `agent_runs` con start/stop/status/summary.
- Reaccionar a `runtime_events` críticos (ej. cookies expiradas →
  pausar `mercadolibre_hunter`).

**Ciclo:**
```
start
  └── lanza agentes en paralelo
        └── cada agente ejecuta su loop interno
              └── reporta heartbeat cada 30s al supervisor
                    └── si timeout → watchdog reinicia
```

## 2. amazon_hunter

**Misión:** descubrir productos con descuento en Amazon MX.

**Comportamiento:**
- Carga seeds de `config/seeds/amazon.json`.
- Frontier en SQLite, no en memoria.
- Browser Playwright con stealth + warmup en homepage.
- Detecta captcha, hace backoff exponencial.
- Para cada producto encontrado:
  - extrae datos (price_parser Amazon)
  - guarda `products` + `price_observation`
  - encola en `intelligence_queue` para evaluación

**Datos de entrada:** `config/seeds/amazon.json`, `config/selectors/amazon.json`,
estado en `frontier.amazon`.

**Datos de salida:** filas en `products`, `price_observations`,
`visited_urls`. Eventos `runtime_events` si captcha persistente o 503.

**Límites:**
- 1-2 páginas paralelas máximo.
- 5-12 segundos entre páginas (jitter).
- Pausa larga (5min) tras 5 captchas seguidos.

## 3. mercadolibre_hunter

**Misión:** descubrir productos ML MX con descuento ≥ 50% y botón Compartir
activo (afiliado).

**Comportamiento:**
- Carga cookies (resolución por env var, igual que legacy).
- Single browser context (cookies son frágiles).
- Lógica de explorers: category, listing, product (reusada del legacy).
- Para cada producto: extrae prices, share button, modal Compartir → affiliate
  link.
- Detecta redirección a `/account-verification` → emite `cookie_expiry` event.

**Datos de entrada:** cookies, `config/seeds/mercadolibre.json`,
`config/selectors/mercadolibre.json`.

**Datos de salida:** `products` con `affiliate_link`, `affiliate_product_id`,
`commission_text`. Eventos de cookie expiry.

**Límites:**
- 1 worker (cookies single-session).
- 1.5-3 segundos entre requests.
- Pausa total tras detectar bloqueo persistente (no quemar cookies).

## 4. telegram_listener

**Misión:** consumir canales de Telegram que reportan ofertas y errores de
precio, parsear mensajes, detectar señales y enviar candidatos a
`price_intelligence`.

**Canales:** los del legacy (`ofertonesmexico`, `OFERTAS PREMIUM MX`,
`OFERTAS RELAMPAGO`) más los configurables por env.

**Reglas:**
- **Ignora** links de Mercado Libre que vengan de Telegram.
- Resuelve shortlinks (bit.ly, goo.gl, etc.) antes de procesar.
- Descarga imagen si el mensaje la trae adjunta.
- Emite `telegram_message` row + `candidate` para evaluación.

**Backfill:** al arrancar y cada `MESSAGE_BACKFILL_POLL_SECONDS` recupera
mensajes perdidos (límite por canal, igual al legacy).

**Workers:** 3 procesando la cola.

## 5. price_intelligence

**Misión:** decidir el destino de cada candidato:

```
publish_immediately | enqueue | save_watchlist | discard
```

**Pipeline:**
1. Normalizar producto.
2. Resolver URL final (HEAD/GET).
3. Extraer precio + imagen + stock.
4. Guardar `price_observation`.
5. Calcular descuento real (vs previous, vs histórico, vs categoría).
6. Calcular `price_error_score` (ver PRICE_ERROR_DETECTION.md).
7. Decidir y registrar `offers` con `state` y `reasons_json`.

## 6. dom_healer

**Misión:** mantener los selectores funcionando.

**Trigger:**
- `degradation_monitor` reporta failure_rate >= threshold.
- Cada agente reporta éxito/fallo de extracción.

**Flujo:**
1. Captura DOM snapshot (`dom_snapshots`).
2. Heurísticas primero: buscar patrones conocidos (ARIA labels, atributos
   data-testid, clases con `price`/`discount`/`saving`).
3. Si las heurísticas no resuelven y `LLM_HEAL_ENABLED=true`, llama a kiro-cli/Anthropic con el snippet.
4. Construye fixture con el snapshot.
5. Corre tests del fixture con los nuevos selectores.
6. Si pasan → aplica al `selectors/<m>.json`, registra
   `selector_versions(test_pass=True)`.
7. Si fallan → revierte, mantiene la versión anterior, registra
   `selector_versions(test_pass=False, reverted_at=now)`.

**Cooldown:** 5min entre intentos por (marketplace, context).

## 7. image_resolver

**Misión:** garantizar que toda oferta publicable tenga imagen válida.

**Estrategia:**
1. Si `products.image_url` existe y resuelve, OK.
2. Si no, buscar en página: `og:image`, microdata, `<img>` principal.
3. Si producto Amazon, fallback a `https://m.media-amazon.com/images/I/<ASIN>.jpg`.
4. Validar `Content-Type: image/*` y tamaño > 5KB.
5. Cachear en `data/images/<sha256>.<ext>`.

Si no logra imagen → marca oferta como `discarded` con razón
`no_image_available`.

## 8. outbox_dispatcher

**Misión:** publicar a WhatsApp respetando todas las reglas.

**Reglas:**
- Cooldown global 5min entre ofertas normales.
- Errores de precio bypass cooldown.
- Si hay >1 oferta elegible al mismo tiempo, pick aleatorio (no FIFO).
- Si una oferta tiene > 1h en outbox → revalidar antes de publicar.
- Si tras revalidación ya no califica → degradar a `expired` con razón.

**Antes de publicar:** validate image, price, stock, URL final.

**Estrictamente serializado**: un solo task `dispatcher_loop`.

## 9. memory_compressor

**Misión:** mantener memoria útil sin que crezca sin control.

**Periodicidad:** cada 6h.

**Tareas:**
- Comprimir tablas con > N filas: `dom_snapshots`, `runtime_events`,
  `discarded_candidates`, `agent_runs` (mantener sólo agregados >7d).
- Generar `memory_summaries` con:
  - patrones de precio por categoría
  - selectores inestables (>3 versiones en 30d)
  - dominios problemáticos
  - razones frecuentes de descarte
  - rangos típicos por marca

## 10. self_repair_agent

**Misión:** aplicar parches mínimos de código cuando detecta bugs específicos
(timeouts crónicos, `KeyError` en estructuras conocidas), siempre con tests.

**Flujo:**
1. Detecta patrón en `runtime_events` (>= N ocurrencias).
2. Genera diff (heurística o LLM).
3. Aplica al fichero, corre `pytest` filtrado.
4. Si pasa → commit local, registro en `self_patches(applied_at, tests_passed)`.
5. Si falla → revierte, registra razón.

**Restricción:** sólo modifica directorios marcados como `auto_patchable=True`
en `config/settings.yaml` (por defecto sólo `marketplaces/*/selectors.json`
y `marketplaces/*/price_parser.py`). El resto requiere intervención humana.

## 11. runtime_watchdog

**Misión:** mantener al bot vivo.

**Comportamiento:**
- Polling cada 30s.
- Cada agente hace heartbeat en `agent_runs.last_heartbeat`.
- Si un agente lleva > 2 ciclos sin heartbeat → reinicio del task.
- Si reinicios consecutivos > 3 → degradar agente (apagado), notificar
  `runtime_event(severity=critical)`.
- Detecta cookies expiradas y pausa el hunter afectado.
- Detecta storage corrupto, errores de DB, espacio en disco.
