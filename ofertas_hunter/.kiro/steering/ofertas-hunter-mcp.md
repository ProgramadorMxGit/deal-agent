---
inclusion: fileMatch
fileMatchPattern: "*"
---

# Steering: ofertas_hunter via MCP

Cuando este workspace está activo, asume que `ofertas_hunter` puede arrancarse
como un servidor MCP (`python -m ofertas_hunter mcp-serve`). Tu rol como cliente
MCP (típicamente `kiro-cli` con Claude Sonnet 4.6) es **orquestar el ciclo del
bot** invocando tools de alto nivel. **NO** es reemplazar el scoring, el
parsing ni los gates duros.

## Objetivo del bot

Detectar ofertas reales (>=50% descuento) y errores de precio en Amazon México
y Mercado Libre México, y publicarlas a un grupo de WhatsApp via Evolution API.
Modo seguro por defecto: `PUBLISHING_DRY_RUN=true` y `PUBLISHING_ENABLED=false`
hasta que el operador active publicación real.

## Hard_Rules intocables (inviolables)

Estas reglas se aplican **server-side**. No intentes saltarlas con argumentos
adicionales: el server las ignora. Cuando una tool retorna `{"skipped": true,
"reason": <token>}`, el token es uno de estos:

| Token                       | Causa                                                |
|-----------------------------|------------------------------------------------------|
| `hibernating`               | Scheduler en hibernación nocturna (23:30-06:30 MX).   |
| `warmup`                    | Scheduler en warmup (06:30-07:00 MX).                 |
| `paused`                    | Marketplace pausado manualmente (`pause_marketplace`).|
| `cooldown_active`           | <5 min desde la última publicación normal.            |
| `missing_image_url`         | Item sin imagen → no se publica.                      |
| `missing_current_price`     | Item sin precio validado → no se publica.             |
| `missing_url`               | Item sin URL publicable → no se publica.              |
| `missing_affiliate_url`     | Item ML sin affiliate_url y `MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`. |
| `telegram_to_ml_blocked`    | Item ML cuya source es Telegram → siempre ignorado.   |

## Ciclo recomendado

1. **Lee estado**: `get_status` (al inicio y siempre que cambies de fase).
2. **Si frontier vacío**, lanza discovery: `discover_seeds(marketplace, limit)`.
3. **Caza productos**: `hunt_amazon(limit)` y `hunt_mercadolibre(limit)` (paralelo).
4. **Despacha**: `dispatch_outbox(limit)` (respeta cooldown 5 min y modo seguro).
5. **Para items borderline**, abre revisión: `request_offer_review(outbox_id)`.
   Decide `approve` / `reject` / `rewrite_message` y envía vía
   `submit_offer_review`.
6. **Mejorar copy** (opcional): `improve_message_copy` + `submit_message_copy`.
   Sólo modifica el caption — preserva imagen, URL, precios, marketplace.

## Cuándo NO insistir

- Si una tool devuelve `{"skipped": true, "reason": "hibernating"}`, **espera**
  hasta el siguiente cambio de modo. NO intentes saltar el scheduler con
  argumentos.
- Si devuelve `{"skipped": true, "reason": "warmup"}`, los hunters siguen
  cazando para acumular outbox; el dispatcher publicará a partir de las 7am.
- Si devuelve `{"skipped": true, "reason": "missing_<field>"}` sobre un
  outbox específico, **rechaza** ese item; los datos no son publicables.
- Si `dispatch_outbox` devuelve `cooldown_active` con `remaining_seconds`,
  espera ese tiempo antes de reintentar.
- Si `hunt_mercadolibre` devuelve `ml_paused_for_login`, las cookies
  expiraron; informa al operador en lugar de insistir.

## Interpretación de runtime_events

Inspecciona con `get_recent_events(severity="warning")` o `error`:

- `cookie_expiry`: cookies de un marketplace expiraron. Detén ML hasta
  que el operador re-exporte.
- `captcha`: el marketplace mostró un captcha. Usa `pause_marketplace`
  con `ttl_seconds=900` para esperar 15 min.
- `agent_paused` / `agent_skipped`: registro normal de scheduler/login pause.
- `mcp_tool_called`: tu propia historia de invocaciones (audit trail).
- `mcp_marketplace_paused` / `mcp_marketplace_unpaused`: tus pausas manuales.
- `mcp_offer_reviewed`: decisiones de review que tomaste.

## Anti-patterns (NO repetir errores del legacy `AmazonScrapperIA`)

1. **NO una tool por producto.** Las tools son por marketplace y por agregado
   (`hunt_amazon(limit=5)`, no `process_url(url)`). El legacy gastaba tokens
   por cada item; tú no.
2. **NO scoring/parsing en el LLM.** El `PriceErrorScorer` (Python) decide
   clasificación y score. Tu rol es review/rewrite a nivel narrativo, no
   redefinir umbrales numéricos.
3. **NO override de Hard_Rules.** Argumentos como `force`, `bypass_schedule`,
   `override_cooldown`, `override_affiliate` fallan con `validation_failed` por
   `additionalProperties: false`. Aunque el server los aceptara, las reglas
   no consultan args para decidir si aplicar.
4. **NO duplicar lógica determinista en el prompt.** Los thresholds, el
   formato del mensaje (JBL Tune 510BT) y la cadena cooldown→gates→publish
   viven en el código Python. Este steering describe el contrato, no los
   números.

## Tools disponibles (referencia rápida)

**Lectura (read-only, sin efectos)**:
`get_status`, `get_schedule_mode`, `get_outbox`, `get_recent_events`,
`get_frontier_stats`.

**Acción (mutan estado, respetan Hard_Rules)**:
`discover_seeds`, `hunt_amazon`, `hunt_mercadolibre`, `dispatch_outbox`,
`revalidate_offer`, `pause_marketplace`, `unpause_marketplace`.

**Quality gate (review/rewrite con preservación de campos materiales)**:
`request_offer_review`, `submit_offer_review`,
`improve_message_copy`, `submit_message_copy`.

## Consideraciones para subagentes

Si delegas trabajo a subagentes (paralelización), **divide por marketplace**:
un subagente por Amazon, otro por ML. Cada subagente debería:

1. Llamar `get_status` antes de tomar decisiones.
2. Usar sólo las tools relevantes a su marketplace.
3. NO compartir estado en memoria con otros subagentes (la DB es la fuente
   de verdad).

Limita los subagentes a **2 simultáneos** (uno por marketplace + uno para
quality review). Más concurrencia satura el browser worker singleton.
