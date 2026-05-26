# Requirements Document

## Introduction

Esta feature integra `kiro-cli` (Claude Sonnet 4.6) como orquestador externo del bot `ofertas_hunter` mediante el protocolo MCP (Model Context Protocol). El bot expone un servidor MCP por stdio a través de un nuevo subcomando `python -m ofertas_hunter mcp-serve`. `kiro-cli` actúa como cliente MCP, arrancando el subproceso del bot y tomando decisiones de alto nivel sobre el ciclo (cuándo descubrir, cazar, despachar) y sobre la calidad de ofertas en el filo (review, rewrite, reject).

El scoring, el parsing y los gates duros (cooldown, imagen+precio+url, afiliado ML, modo seguro, autoridad del scheduler) permanecen 100 % en Python y son inviolables: el servidor MCP los aplica server-side aunque `kiro-cli` los pida saltar. El comando `python -m ofertas_hunter run` sigue funcionando intacto como fallback.

## Glossary

- **Bot**: Aplicación Python `ofertas_hunter` existente, con su orchestrator, agentes, scheduler, watchdog, dispatcher y publisher.
- **MCP_Server**: Punto MCP server-side expuesto por el Bot al ejecutar `python -m ofertas_hunter mcp-serve`. Habla MCP por stdio y expone las tools descritas en este documento.
- **MCP_Client**: Cliente MCP externo (`kiro-cli` corriendo Claude Sonnet 4.6) que se conecta al MCP_Server, lista sus tools y las invoca.
- **Hard_Rules**: Conjunto de invariantes inviolables aplicadas server-side por el Bot, sin importar lo que pida el MCP_Client. Incluye: cooldown global del dispatcher de 5 minutos para ofertas `normal`; gate imagen + precio + URL para ofertas `normal`; URL de afiliado de Mercado Libre obligatoria cuando `MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`; modo seguro por defecto (`PUBLISHING_ENABLED=false` y `PUBLISHING_DRY_RUN=true`); decisiones del Schedule_Authority; y el filtro Telegram→Mercado Libre ya implementado.
- **Borderline_Offer**: `OutboxItem` que pasa los gates duros (imagen, precio, URL, afiliado cuando aplica) pero cuyo scoring queda dentro de un margen configurable respecto al umbral de publicación, requiriendo una decisión adicional de revisión antes de publicar.
- **Schedule_Authority**: Decisión runtime del `OperatingScheduler` (`hibernating | warmup | active`) que el MCP_Server trata como autoridad final para hunts, discovery y dispatch. Ningún parámetro del MCP_Client la sobreescribe.
- **OutboxItem**: Modelo existente del bot que representa una oferta encolada para publicación.
- **Runtime_Event**: Entrada de auditoría existente persistida vía `emit_runtime_event` en la tabla `runtime_events`.

## Requirements

### Requirement 1: Lifecycle del MCP server y configuración del MCP_Client

**User Story:** Como usuario, quiero configurar `kiro-cli` para hablar con el bot vía MCP, para no tener que reescribir el orchestrator existente.

#### Acceptance Criteria

1. THE Bot SHALL exponer un subcomando CLI nuevo `python -m ofertas_hunter mcp-serve` que arranca un servidor MCP sobre stdio.
2. WHEN `python -m ofertas_hunter mcp-serve` se invoca, THE MCP_Server SHALL completar el handshake MCP con cualquier MCP_Client conformante por stdio.
3. THE MCP_Server SHALL anunciar al MCP_Client durante el handshake la lista de tools definidas en los Requirements 2 a 7.
4. WHERE el usuario provee una entrada de configuración MCP que apunta a `python -m ofertas_hunter mcp-serve`, THE kiro-cli SHALL poder arrancar el subproceso del Bot y conectarse al MCP_Server sin pasos manuales adicionales.
5. THE Bot SHALL incluir como entregable un fragmento de configuración MCP de ejemplo apto para `~/.kiro/settings/mcp.json`.

### Requirement 2: Tools de lectura sin efectos secundarios

**User Story:** Como `kiro-cli`, quiero leer el estado runtime del Bot sin afectar producción, para decidir el siguiente paso del ciclo.

#### Acceptance Criteria

1. THE MCP_Server SHALL exponer la tool `get_status` que devuelve el modo actual del Schedule_Authority, el valor de `publishing_enabled`, el valor de `publishing_dry_run` y los nombres de los agentes registrados.
2. THE MCP_Server SHALL exponer la tool `get_outbox(limit, type_filter)` que devuelve los OutboxItems más recientes filtrados por `type_filter` y limitados por `limit`.
3. THE MCP_Server SHALL exponer la tool `get_recent_events(limit, severity)` que devuelve Runtime_Events filtrados por `severity` y limitados por `limit`.
4. THE MCP_Server SHALL exponer la tool `get_frontier_stats(marketplace)` que devuelve los conteos del frontier agrupados por estado para el marketplace solicitado.
5. THE MCP_Server SHALL exponer la tool `get_schedule_mode` que devuelve la decisión actual del Schedule_Authority incluyendo modo y tiempo restante hasta el próximo cambio.
6. WHEN cualquier tool de lectura es invocada, THE MCP_Server SHALL NOT modificar el outbox, el frontier, la tabla `published_messages` ni ninguna otra tabla persistente.

### Requirement 3: Tools de acción que respetan el Schedule_Authority

**User Story:** Como `kiro-cli`, quiero disparar acciones de hunt, discovery y dispatch, para conducir el ciclo del Bot respetando la autoridad del scheduler.

#### Acceptance Criteria

1. THE MCP_Server SHALL exponer las tools de acción `discover_seeds(marketplace, limit)`, `hunt_amazon(limit)`, `hunt_mercadolibre(limit)` y `dispatch_outbox(limit)`.
2. WHEN se invoca `hunt_amazon`, `hunt_mercadolibre`, `discover_seeds` o `dispatch_outbox` WHILE el Schedule_Authority decide `hibernating` o `warmup` para esa operación, THE MCP_Server SHALL devolver `{"skipped": true, "reason": "hibernating"}` o `{"skipped": true, "reason": "warmup"}` según corresponda y SHALL NOT ejecutar el trabajo subyacente.
3. THE MCP_Server SHALL exponer la tool `revalidate_offer(outbox_id)` que ejecuta el `PlaywrightRevalidator` existente sobre el item indicado y devuelve clasificación, `confidence_label` y razón de descarte si aplica.
4. THE MCP_Server SHALL exponer las tools `pause_marketplace(name, reason, ttl_seconds)` y `unpause_marketplace(name)` que activan o desactivan un flag de pausa en proceso que afecta a las siguientes invocaciones de hunt y discover para ese marketplace.
5. WHEN `pause_marketplace` se invoca con `ttl_seconds > 0`, THE MCP_Server SHALL limpiar el flag de pausa automáticamente al cumplirse `ttl_seconds`.
6. WHEN cualquier tool de acción se ejecuta, THE MCP_Server SHALL reutilizar los agentes existentes del Bot, el browser worker compartido, el scheduler y la conexión a la base de datos en lugar de instanciar scrapers paralelos.

### Requirement 4: Quality gate para Borderline_Offers

**User Story:** Como `kiro-cli`, quiero revisar Borderline_Offers antes de que se publiquen, para aprobar, rechazar o reescribir su copy sin saltar las Hard_Rules.

#### Acceptance Criteria

1. THE MCP_Server SHALL exponer la tool `request_offer_review(outbox_id)` que devuelve el payload completo del OutboxItem (título, precios, `image_url`, `url`, marketplace, clasificación, score, razones) más la previsualización del mensaje formateado que se publicaría.
2. THE tool `request_offer_review` SHALL aceptar una respuesta estructurada del MCP_Client cuyo campo `decision` es exactamente uno de `approve`, `reject` o `rewrite_message`.
3. WHEN el MCP_Client responde con `decision="approve"`, THE MCP_Server SHALL marcar el OutboxItem como elegible para el siguiente tick del dispatcher sin alterar su payload.
4. WHEN el MCP_Client responde con `decision="reject"` con un campo `reason`, THE MCP_Server SHALL marcar el OutboxItem como descartado con esa razón.
5. WHEN el MCP_Client responde con `decision="rewrite_message"` con un campo `new_text`, THE MCP_Server SHALL actualizar únicamente el caption renderizado del OutboxItem y SHALL preservar `image_url`, `url`, `current_price`, `previous_price`, `discount_percent` y `marketplace`.
6. IF la respuesta de revisión carece de campos obligatorios o el `decision` es desconocido, THEN THE MCP_Server SHALL dejar el OutboxItem sin cambios y devolver un resultado de error que describe la validación fallida.

### Requirement 5: Mejora opt-in del copy del mensaje

**User Story:** Como `kiro-cli`, quiero mejorar el copy del mensaje saliente bajo demanda, para refinar el wording sin tocar scoring ni gates.

#### Acceptance Criteria

1. THE MCP_Server SHALL exponer la tool `improve_message_copy(outbox_id, current_text)` que devuelve el contexto del OutboxItem necesario para reescribir el copy y acepta `new_text` como respuesta.
2. WHEN `improve_message_copy` se invoca, THE MCP_Server SHALL modificar únicamente el caption renderizado del OutboxItem indicado y SHALL NOT alterar `image_url`, `url`, precios, clasificación ni score.
3. WHERE `improve_message_copy` se invoca sobre un OutboxItem cuyos gates duros no se cumplen, THE MCP_Server SHALL rechazar la reescritura y devolver un resultado de error con el nombre del gate fallido.

### Requirement 6: Aplicación server-side de las Hard_Rules

**User Story:** Como mantenedor del sistema, quiero que las Hard_Rules se apliquen server-side, para que `kiro-cli` no pueda accidentalmente publicar ofertas que las violen.

#### Acceptance Criteria

1. WHEN una tool MCP llevaría a publicar un OutboxItem de tipo `normal` y el cooldown global de 5 minutos no ha transcurrido desde la última publicación normal exitosa, THE MCP_Server SHALL devolver `{"skipped": true, "reason": "cooldown_active"}` y SHALL NOT publicar el item.
2. WHEN una tool MCP llevaría a publicar un OutboxItem cuyo payload carece de `image_url`, `current_price` o URL publicable, THE MCP_Server SHALL rehusar publicar ese item y devolver un resultado de error que nombra el campo faltante.
3. WHILE `MERCADOLIBRE_AFFILIATE_REQUIRED_FOR_PUBLISH=true`, IF una tool MCP intenta publicar un item de Mercado Libre sin `affiliate_url`, THEN THE MCP_Server SHALL rehusar publicar y devolver `{"error": "missing_affiliate_url"}`.
4. WHILE `PUBLISHING_ENABLED=false` o `PUBLISHING_DRY_RUN=true`, THE MCP_Server SHALL comportarse exactamente igual que el dispatcher existente en modo seguro y SHALL devolver `dry_run=true` en el resultado de la acción en lugar de llamar a Evolution API.
5. WHEN una tool MCP solicita una acción cuyo Schedule_Authority no resuelve a `active`, THE MCP_Server SHALL devolver `{"skipped": true, "reason": "<mode>"}` y SHALL NOT aceptar ningún flag de override del MCP_Client.
6. WHEN una tool MCP intenta publicar un enlace de Mercado Libre cuya fuente registrada es Telegram, THE MCP_Server SHALL ignorar la publicación replicando la regla existente `telegram_ignore_mercadolibre_links`.

### Requirement 7: Observabilidad de cada llamada MCP

**User Story:** Como mantenedor del sistema, quiero que cada llamada MCP quede auditada en `runtime_events`, para revisar las decisiones de `kiro-cli` después del hecho.

#### Acceptance Criteria

1. WHEN cualquier tool MCP es invocada, THE MCP_Server SHALL emitir un Runtime_Event con `kind="mcp_tool_called"`, `severity="info"` y `payload={"tool": <nombre>, "args_summary": <resumen seguro>, "result_summary": <resumen seguro>}`.
2. THE MCP_Server SHALL truncar `args_summary` y `result_summary` a un máximo documentado y SHALL excluir secretos como cookies, API keys o tokens de sesión.
3. IF una tool MCP falla con una excepción, THEN THE MCP_Server SHALL emitir un Runtime_Event con `severity="error"` y un payload que incluye el nombre de la tool y el nombre de la clase de la excepción.

### Requirement 8: Coexistencia con `python -m ofertas_hunter run`

**User Story:** Como usuario, quiero que `python -m ofertas_hunter run` siga funcionando sin cambios, para tener un fallback si `kiro-cli` no está disponible.

#### Acceptance Criteria

1. THE Bot SHALL preservar el comando `python -m ofertas_hunter run` y SHALL producir el mismo comportamiento del orchestrator que antes de esta feature.
2. THE Bot SHALL mantener sin cambios los defaults de `OrchestratorConfig`, las ventanas del scheduler, el `dispatcher_loop_interval` y las políticas del watchdog.
3. WHEN `python -m ofertas_hunter run` y `python -m ofertas_hunter mcp-serve` se lanzan simultáneamente contra la misma base de datos, THE MCP_Server SHALL detectar la concurrencia y SHALL rehusar arrancar con un mensaje de error que describe el conflicto.

### Requirement 9: Steering scoped al workspace para el MCP_Client

**User Story:** Como mantenedor del sistema, quiero un steering que explique al modelo las invariantes del Bot, para que no insista en acciones que violan Hard_Rules.

#### Acceptance Criteria

1. THE Bot SHALL incluir un archivo de steering en `.kiro/steering/ofertas-hunter-mcp.md` con scope de workspace.
2. THE archivo de steering SHALL documentar el objetivo del Bot, las Hard_Rules de este documento, la estrategia recomendada de ciclo, los escenarios en los que el modelo SHALL NOT reintentar la misma acción y la interpretación de los `Runtime_Event.kind` más comunes.
3. THE archivo de steering SHALL documentar los anti-patterns heredados del proyecto legacy `AmazonScrapperIA` que el modelo SHALL NOT repetir: tools por producto, duplicación de lógica determinista en el prompt, y scoring o parsing en el LLM.

### Requirement 10: Invariantes del test suite y diagnostics

**User Story:** Como mantenedor del sistema, quiero que el test suite siga verde y sin diagnostics, para que esta feature no regrese comportamiento previo.

#### Acceptance Criteria

1. WHEN el test suite del proyecto se ejecuta tras implementar esta feature, THE Bot SHALL mantener los 344 tests previamente verdes en estado passing.
2. THE Bot SHALL incluir tests unitarios para cada nueva tool MCP que cubren tanto el camino feliz como el rechazo por Hard_Rules.
3. THE Bot SHALL incluir un smoke test que verifica el handshake MCP contra un MCP_Client en proceso y SHALL listar las tools anunciadas.
4. WHEN se recolectan diagnostics tras integrar esta feature, THE Bot SHALL reportar cero diagnostics nuevos en los archivos modificados.
