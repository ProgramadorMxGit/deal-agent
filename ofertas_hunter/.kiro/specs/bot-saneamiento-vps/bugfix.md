# Bugfix Requirements Document

## Introduction

El bot `ofertas_hunter` desplegado en el VPS GCP (`agaetranahoy@34.59.242.95:/opt/deal-agent/ofertas_hunter`) presenta un deterioro operativo severo tras semanas de ejecución continua y la introducción reciente del feature `DiversityCurator` (commits `935cdf6..a848f8f`). El usuario reporta que "el bot dejó de publicar ofertas", aunque la realidad observada es que **sí publica pero con latencia excesiva** (última publicación exitosa hace ~3.5 horas, versus el ritmo esperado dentro del cooldown de 5 min).

El diagnóstico verificado vía SSH y consultas SQL directas reveló cuatro defectos concurrentes que componen un único bug operativo:

1. La base de datos `data/ofertas_hunter.db` creció a **6.0 GB**, de los cuales **~4 GB** corresponden a la tabla de telemetría `runtime_events` (~21,373,417 filas).
2. La tabla `runtime_events` carece de los índices necesarios sobre `kind` y `created_at` aislados, por lo que cualquier consulta del orquestador IA con filtro `WHERE kind=...` o `LIKE '%hunt%'` recorre 21M filas y excede los timeouts del MCP server.
3. El proceso `kiro-cli --agent ofertas-orquestador` (modo [1] de `start.sh`) se ejecuta en foreground dentro de la sesión SSH cuando no hay `DISPLAY`, por lo que recibe `SIGHUP` y muere al cerrar la terminal Windows del operador.
4. El componente `MaintenanceCompressor` ya existente (`src/ofertas_hunter/maintenance/compressor.py`, comando `python -m ofertas_hunter compress-memory`) **nunca se invoca automáticamente**: no hay cron, ni systemd timer, ni job loop interno que lo dispare.

El impacto compuesto es que el bot publica con latencia severa, las consultas del orquestador degradan exponencialmente, y cada cierre accidental de la terminal del operador deja la operación detenida hasta el siguiente login manual. La corrección debe sanear el estado actual sin perder datos del negocio y debe instalar mecanismos preventivos que impidan la recurrencia.

## Bug Analysis

### Current Behavior (Defect)

Estado actual observado del bot en el VPS, agrupado por la dimensión que se rompe.

**Tamaño y consultas de la base de datos:**

1.1 WHEN el bot lleva semanas publicando ofertas y registrando telemetría THEN el archivo `data/ofertas_hunter.db` crece sin límite hasta superar 6 GB y la tabla `runtime_events` acumula más de 21 millones de filas sin purga automática
1.2 WHEN el orquestador IA o cualquier MCP tool ejecuta una consulta con filtro `WHERE kind = ?` o `LIKE '%...%'` sobre `runtime_events` THEN el motor SQLite ejecuta full table scan sobre 21M filas y la consulta excede el timeout, ya que el único índice presente es `idx_runtime_events_severity (severity, created_at DESC)` y no existe índice sobre `kind` ni sobre `created_at` aislado
1.3 WHEN el operador inspecciona el peso por tabla con `dbstat` THEN observa que `runtime_events` ocupa ~4 GB de los 6 GB totales del archivo y existe espacio físico no liberado pendiente de `VACUUM`

**Persistencia del proceso del orquestador IA:**

1.4 WHEN el operador inicia el bot vía `start.sh` modo [1] (orquestador IA principal) en una sesión SSH sin `DISPLAY` THEN el script lanza `kiro-cli --classic chat --agent ofertas-orquestador --trust-all-tools "..."` en foreground dentro de la sesión SSH en lugar de delegarlo a `tmux` o a un servicio persistente
1.5 WHEN el operador cierra la terminal Windows o la sesión SSH se interrumpe por red THEN el proceso `kiro-cli` recibe `SIGHUP`, muere, y arrastra consigo al MCP server y a los hunters de Playwright, deteniendo toda publicación de ofertas hasta el siguiente login manual

**Mantenimiento automático ausente:**

1.6 WHEN transcurren 6 o más horas desde el último arranque del bot THEN ningún mecanismo del sistema invoca `python -m ofertas_hunter compress-memory`, por lo que las políticas de retención del `MaintenanceCompressor` (TTL de `runtime_events`, top-N de `dom_snapshots`, TTL+cap de `discarded_candidates`, top-N de `agent_runs`) nunca se aplican en producción
1.7 WHEN el tamaño físico de `data/ofertas_hunter.db` supera un umbral operativo (por ejemplo 1 GB) THEN el sistema no emite ningún `runtime_event` de aviso ni alerta, y el operador sólo se entera al observar lentitud severa en la publicación

### Expected Behavior (Correct)

Comportamiento que debe observarse después del fix, en el mismo orden que los defectos.

**Tamaño y consultas de la base de datos:**

2.1 WHEN el bot lleva semanas publicando ofertas y registrando telemetría THEN el archivo `data/ofertas_hunter.db` SHALL mantenerse por debajo de 500 MB en estado estable (objetivo ideal < 200 MB) gracias a la aplicación periódica de las políticas de retención del `MaintenanceCompressor`, y la tabla `runtime_events` SHALL conservar únicamente los eventos dentro del TTL configurado (por defecto 14 días en este saneamiento, ajustable vía configuración)
2.2 WHEN el orquestador IA o cualquier MCP tool ejecuta una consulta con filtro `WHERE kind = ?` o con orden por `created_at` sobre `runtime_events` THEN el plan de ejecución (`EXPLAIN QUERY PLAN`) SHALL mostrar uso de un índice dedicado (`idx_runtime_events_kind` sobre `(kind, created_at DESC)` y `idx_runtime_events_created_at` sobre `(created_at)`) y la consulta SHALL completarse en menos de 100 ms para volúmenes operativos esperados
2.3 WHEN el operador inspecciona el peso por tabla con `dbstat` después del saneamiento THEN `runtime_events` SHALL ocupar una fracción acotada del archivo y el espacio físico previamente reservado SHALL haber sido devuelto al sistema de archivos mediante `VACUUM`

**Persistencia del proceso del orquestador IA:**

2.4 WHEN el operador inicia el bot vía `start.sh` modo [1] en una sesión SSH sin `DISPLAY` THEN el script SHALL lanzar el orquestador IA dentro de un mecanismo persistente (sesión `tmux` desacoplada y/o servicio `systemd`) que sobreviva al cierre de la sesión SSH iniciadora, de manera análoga al tratamiento que ya recibe el modo [2] (subagentes) en `start.sh`
2.5 WHEN el operador cierra la terminal Windows o la sesión SSH se interrumpe THEN el proceso del orquestador IA, el MCP server y los hunters SHALL continuar ejecutándose, y al reconectar vía SSH el operador SHALL poder reattachar (por ejemplo `tmux attach -t ofertas_orquestador`) o consultar logs (`journalctl`) sin haber perdido la continuidad de la publicación

**Mantenimiento automático presente:**

2.6 WHEN transcurren 6 horas desde la última ejecución de `compress-memory` THEN un mecanismo automático del sistema (systemd timer o equivalente) SHALL invocar `python -m ofertas_hunter compress-memory` con las políticas configuradas, aplicando TTL de `runtime_events`, top-N de `dom_snapshots`, TTL y cap de `discarded_candidates` y top-N de `agent_runs`
2.7 WHEN el tamaño físico de `data/ofertas_hunter.db` supera el umbral configurado después de una ejecución de compresión THEN el sistema SHALL emitir un `runtime_event` con `kind="db_size_warning"` que registre el tamaño actual y permita al operador detectar el problema antes de que degrade la publicación

### Unchanged Behavior (Regression Prevention)

Comportamiento existente que debe preservarse intacto. Cualquier cambio que rompa una de estas cláusulas es una regresión inaceptable.

**Datos del negocio:**

3.1 WHEN el saneamiento purga telemetría y snapshots THEN el sistema SHALL CONTINUE TO conservar íntegros los registros de las tablas de negocio: `published_messages`, `outbox`, `products`, `frontier`, `offers`, `agent_runs` (manteniendo al menos los 1000 más recientes según política), `price_observations` y `ml_session_state`, sin pérdida ni truncamiento
3.2 WHEN se purga `runtime_events` por TTL y `dom_snapshots` por top-N THEN el sistema SHALL CONTINUE TO conservar al menos los `dom_snapshots` más recientes (top 200 por defecto) y los `runtime_events` dentro de la ventana de TTL configurada, ya que estos son insumo de auto-healing y diagnóstico
3.3 WHEN el saneamiento ejecuta `VACUUM` o cualquier operación destructiva sobre la base de datos THEN el sistema SHALL CONTINUE TO disponer de un backup completo previo en `data/ofertas_hunter.db.backup-YYYYMMDD-HHMMSS` que permita restaurar el estado anterior sin pérdida

**Ciclo operativo del orquestador y hunters:**

3.4 WHEN el orquestador IA está activo bajo el nuevo régimen persistente THEN el sistema SHALL CONTINUE TO ejecutar el ciclo continuo `get_status → discover_seeds → hunt_* → dispatch_outbox → ...` sin alteración del flujo ni del prompt del agente
3.5 WHEN el dispatcher procesa el outbox THEN el sistema SHALL CONTINUE TO inyectar `DiversityCurator` como `item_selector` con la configuración existente activada vía `DIVERSITY_CURATOR_ENABLED=true`, sin alterar el comportamiento del feature recientemente desplegado
3.6 WHEN el frontier de un marketplace está vacío o subseedeado THEN el sistema SHALL CONTINUE TO realizar auto-seed desde `config/seeds/<marketplace>.json` para Amazon y MercadoLibre
3.7 WHEN el webhook `/cookies_ml` recibe cookies actualizadas THEN el sistema SHALL CONTINUE TO realizar ML session recovery y hot-reload de cookies sin reiniciar el bot
3.8 WHEN Amazon legacy hunter (anti-captcha) está activado por configuración THEN el sistema SHALL CONTINUE TO ejecutarlo sin cambios en su lógica ni en sus reintentos

**Reglas temporales y de publicación:**

3.9 WHEN la hora local CDMX cae en `23:30-06:30` THEN el sistema SHALL CONTINUE TO entrar en estado `hibernating`; en `06:30-07:00` SHALL CONTINUE TO ejecutar `warmup`; y en `07:00-23:30` SHALL CONTINUE TO operar en estado `active`
3.10 WHEN se publica una oferta normal THEN el sistema SHALL CONTINUE TO respetar el cooldown de 5 minutos antes de la siguiente publicación equivalente
3.11 WHEN se detecta un error de precio (price error) THEN el sistema SHALL CONTINUE TO aplicar el bypass de cooldown ya implementado, publicando sin esperar el cooldown estándar

**Suite de tests y modos de ejecución:**

3.12 WHEN se ejecuta la suite de tests existente local o en el VPS THEN el sistema SHALL CONTINUE TO pasar los 711 tests sin regresiones, y los nuevos tests añadidos por el saneamiento (índices vía `EXPLAIN QUERY PLAN`, `MaintenanceCompressor` sobre dataset sintético, emisión de `db_size_warning`) SHALL integrarse a la suite sin romperla
3.13 WHEN el operador inicia el bot vía `start.sh` modo [3] (Python sin IA, gestionado por systemd) THEN el sistema SHALL CONTINUE TO funcionar exactamente igual que antes, sin cambios en su unidad systemd ni en su flujo
3.14 WHEN el operador inicia el bot vía `start.sh` modo [2] (subagentes) que ya emplea `tmux` cuando no hay `DISPLAY` THEN el sistema SHALL CONTINUE TO comportarse exactamente igual que antes del saneamiento

**Código y arquitectura:**

3.15 WHEN se aplica el saneamiento THEN el sistema SHALL CONTINUE TO conservar todo el código fuente Python existente sin refactors ni borrados de archivos `.py`; los cambios SHALL limitarse a añadir scripts de operación, unidades systemd, ajustes mínimos a `start.sh` y, si procede, configuración para los TTLs del compressor
