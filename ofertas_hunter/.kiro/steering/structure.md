---
inclusion: fileMatch
fileMatchPattern: "*"
---

# Arquitectura: ofertas_hunter

## Dos modos de ejecucion

### 1. `python -m ofertas_hunter run` (daemon clasico, fallback)

Arranca el orchestrator completo con todos los agentes en el mismo proceso:

- DiscoveryAgent (Amazon + ML)
- AmazonHunterAgent + MercadoLibreHunterAgent
- OutboxDispatcher
- TelegramListenerAgent
- MaintenanceAgent (memoria, watchdog)

Loop cooperativo basado en `OperatingScheduler`. Sin IA, todo en reglas Python.

### 2. `python -m ofertas_hunter mcp-serve` (servidor MCP)

Expone los mismos componentes via 16 tools MCP por stdio. Cliente MCP (kiro-cli) toma
las decisiones de alto nivel (cuando hunt, cuando dispatch, que reescribir).

**No instancia agentes nuevos**: reusa los singletons del Orchestrator (BrowserWorker,
EvolutionClient, OutboxDispatcher, OperatingScheduler).

## 16 Tools MCP

### Lectura (read-only, sin efectos)

| Tool                  | Resumen                                                        |
|-----------------------|----------------------------------------------------------------|
| `get_status`          | Modo del scheduler, flags publishing, agentes registrados.     |
| `get_schedule_mode`   | Modo actual + segundos hasta el proximo cambio.                |
| `get_outbox`          | Items recientes del outbox, filtrable por tipo.                |
| `get_recent_events`   | Runtime events recientes, filtrable por severity.              |
| `get_frontier_stats`  | Conteo del frontier por kind para un marketplace.              |

### Accion (mutan estado, respetan Hard_Rules)

| Tool                    | Resumen                                                                  |
|-------------------------|--------------------------------------------------------------------------|
| `discover_seeds`        | Procesa URLs listing/category/deals del frontier; mete productos.        |
| `hunt_amazon`           | Procesa N URLs `kind=product` del frontier Amazon.                       |
| `hunt_mercadolibre`     | Procesa N URLs `kind=product` del frontier ML. Devuelve ml_paused_for_login si cookies expiraron. |
| `dispatch_outbox`       | Despacha items pendientes. Respeta cooldown 5 min, gates, modo seguro.   |
| `revalidate_offer`      | Re-abre el producto con Playwright para revalidar.                       |
| `pause_marketplace`     | Pausa hunts/discovery del marketplace. TTL opcional.                     |
| `unpause_marketplace`   | Despausa.                                                                |

### Calidad (review/rewrite con preservacion de campos materiales)

| Tool                      | Resumen                                                                  |
|---------------------------|--------------------------------------------------------------------------|
| `request_offer_review`    | Devuelve payload + review_token. Aplica gates antes de crear sesion.     |
| `submit_offer_review`     | Aplica decision: approve / reject / rewrite_message. Solo modifica caption_override. |
| `improve_message_copy`    | Devuelve contexto + review_token para reescribir el caption.             |
| `submit_message_copy`     | Aplica el rewrite. Solo modifica caption_override.                       |

## Hard Rules (server-side, inviolables)

Aplicadas en `mcp/safety.py` antes de cada tool de accion:

1. **schedule_authority** — si scheduler != active, skip con reason del modo.
2. **marketplace_paused** — si el marketplace esta pausado, skip.
3. **cooldown_normal** — si <5 min desde la ultima publicacion normal, skip cooldown_active.
4. **image_price_url** — gate: imagen + precio + url. Skip con reason missing_<field>.
5. **ml_affiliate** — items ML sin affiliate_url, skip missing_affiliate_url.
6. **telegram_to_ml** — items ML originados en Telegram, skip telegram_to_ml_blocked.
7. **publishing_safe_mode** — el publisher devuelve dry_run en lugar de llamar API.

## Outbox (estado de un item)

```
new -> pending -> in_flight -> sent | discarded | retry
                           \-> blocked (gates) -> discarded
```

- `pick_random_eligible` marca `in_flight` atomicamente para evitar duplicados.
- Cooldown se restaura desde `published_messages` al arrancar (no se pierde al reiniciar).
- `_recently_published(asin, hours=48)` evita re-encolar items recientes.

## Lockfile bidireccional

`data/mcp_serve.lock` (scope=run o mcp-serve). Si los dos comandos chocan, el
segundo falla con exit 2 explicando el conflicto.

## Subagentes kiro-cli

5 agentes en `~/.kiro/agents/`:

- **ofertas-orquestador** — coordinador principal.
- **ofertas-amazon** — solo hunt Amazon.
- **ofertas-ml** — solo hunt Mercado Libre.
- **ofertas-qa** — review + rewrite del outbox.
- **ofertas-telegram** — senales de canales Telegram, prioriza errores de precio.

Todos heredan los mcpServers via `includeMcpJson: true` desde
`.kiro/settings/mcp.json` del workspace.

## Loop daemon Python (orquestador_ia.py)

Alternativa sin IA cuando kiro-cli no esta autenticado:

- 3 loops asyncio paralelos (`loop_amazon`, `loop_ml`, `loop_dispatcher`).
- Sin espera entre ciclos (solo 30s si frontier vacio).
- Auto-pausa Amazon 10 min si CAPTCHAs consecutivos.
- Cooldown dispatcher respeta 5 min entre normales.
- `loop_status` cada 5 min imprime tabla Rich con metricas.
