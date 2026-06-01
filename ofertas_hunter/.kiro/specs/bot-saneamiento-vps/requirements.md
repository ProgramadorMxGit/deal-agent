# Requirements Document

## Introduction

`bot-saneamiento-vps` es un proceso independiente del bot principal `ofertas_hunter` cuya responsabilidad es ejecutar tareas de saneamiento (housekeeping) sobre el estado del bot en el VPS. Hoy esa función la cubre una colección dispersa de scripts ad-hoc bajo `scripts/_vps_*.py` (`_vps_cleanup_stale.py`, `_vps_cleanup_duplicates.py`, `_vps_cleanup_bad_discounts.py`, `cleanup_outbox_missing_prev_price.py`), cada uno con su propio entrypoint, su propio acceso a SQLite y su propia convención de logging. Esta feature consolida esas reglas en un módulo Python testeado, con un único entrypoint, sus propios tests, dry-run por defecto, auditoría en `runtime_events` y un servicio `systemd` dedicado.

El alcance se limita estrictamente a paridad con los scripts existentes: stale outbox, duplicados ya publicados, descuentos inconsistentes y items sin `previous_price` / `discount_percent`. No se agregan reglas nuevas (no se toca filesystem, ni VACUUM, ni cookies, ni frontier, ni locks). El bot principal `ofertas_hunter` y todos sus comandos (`run`, `dispatch`, `mcp-serve`, `compress-memory`) siguen funcionando sin cambios.

El bot de saneamiento es un proceso oneshot: cada invocación lee el estado, decide qué tareas aplicar, ejecuta cada tarea de forma idempotente, escribe un reporte y termina. La frecuencia se controla con un `systemd .timer` separado del `ofertas-hunter-maintenance.timer` existente.

## Glossary

- **Bot_Saneamiento**: El proceso oneshot definido por esta feature. Se invoca como `python -m ofertas_hunter saneamiento ...` reusando el paquete existente.
- **Bot_Principal**: La aplicación `ofertas_hunter` ya existente (orchestrator, dispatcher, MCP server, agentes, watchdog).
- **DB**: Base de datos SQLite del Bot_Principal en `data/ofertas_hunter.db` que el Bot_Saneamiento abre en modo lectura/escritura.
- **Saneamiento_Task**: Unidad nombrada de saneamiento (por ejemplo `outbox-stale`, `outbox-duplicates`, `outbox-bad-discounts`, `outbox-missing-prev-price`). Cada Saneamiento_Task encapsula una regla de los scripts `_vps_cleanup_*.py` actuales.
- **Saneamiento_Run**: Una invocación completa del Bot_Saneamiento que ejecuta uno o más Saneamiento_Task en orden y produce un Saneamiento_Report.
- **Saneamiento_Report**: Estructura de datos persistida por cada Saneamiento_Run con el resultado por Saneamiento_Task: filas inspeccionadas, filas afectadas, ids de muestra y modo (dry-run / apply).
- **Dry_Run**: Modo de ejecución por defecto del Bot_Saneamiento en el que las Saneamiento_Task identifican candidatos pero no escriben mutaciones a la DB.
- **Apply_Mode**: Modo de ejecución activado con `--apply` (o equivalente) en el que las Saneamiento_Task pueden mutar el estado de la DB.
- **Stale_Outbox_Item**: `outbox` row con `state='pending'`, `type='normal'` y `enqueued_at` con antigüedad mayor a 4 horas cuyo `message_payload_json` contiene un valor no nulo en `previous_price`. Replica `_vps_cleanup_stale.py`.
- **Published_Duplicate_Outbox_Item**: `outbox` row con `state='pending'` cuyo `message_payload_json.item_id` o `message_payload_json.asin` coincide con el de cualquier `published_messages` row con `success=1` cuyo `sent_at` está dentro de las últimas 48 horas. Replica `_vps_cleanup_duplicates.py`.
- **Bad_Discount_Outbox_Item**: `outbox` row con `state='pending'` cuyo `message_payload_json` tiene `current_price`, `previous_price` y `discount_percent` no nulos, pero o bien `previous_price <= current_price`, o bien la diferencia absoluta entre el porcentaje declarado y el porcentaje recalculado `(previous - current) / previous * 100` excede 3 puntos porcentuales. Replica `_vps_cleanup_bad_discounts.py`.
- **Missing_Prev_Price_Outbox_Item**: `outbox` row con `state='pending'` cuyo `offers.classification` no es exactamente `price_error_confirmed` y cuyo `message_payload_json` no tiene `previous_price` o no tiene `discount_percent` (por la regla de truthy del script original). Replica `cleanup_outbox_missing_prev_price.py`.
- **Runtime_Event**: Entrada de auditoría existente persistida en la tabla `runtime_events` mediante el helper `emit_runtime_event` del Bot_Principal.

## Requirements

### Requirement 1: Entrypoint del Bot_Saneamiento

**User Story:** Como operador, quiero invocar el Bot_Saneamiento con un único comando estable, para no tener que recordar la ruta de cada script ad-hoc.

#### Acceptance Criteria

1. THE Bot_Saneamiento SHALL exponer el subcomando `python -m ofertas_hunter saneamiento` como entrypoint único.
2. THE subcomando `saneamiento` SHALL aceptar un parámetro `--task` que admite los valores literales `outbox-stale`, `outbox-duplicates`, `outbox-bad-discounts`, `outbox-missing-prev-price` y `all`.
3. WHEN `--task` se omite, THE Bot_Saneamiento SHALL ejecutar el conjunto completo de Saneamiento_Task definidas en el Requirement 4 en el orden documentado en el Requirement 4.
4. THE subcomando `saneamiento` SHALL aceptar un flag `--apply` que activa Apply_Mode para esa Saneamiento_Run.
5. WHEN el flag `--apply` no está presente, THE Bot_Saneamiento SHALL ejecutar la Saneamiento_Run en Dry_Run.
6. THE Bot_Saneamiento SHALL aceptar un parámetro `--marketplace` con los valores `amazon`, `mercadolibre` o `all` que filtra los candidatos de cada Saneamiento_Task por el marketplace del producto asociado.
7. WHEN `--marketplace` se omite, THE Bot_Saneamiento SHALL aplicar las Saneamiento_Task a candidatos de cualquier marketplace.
8. WHEN el Bot_Saneamiento termina una Saneamiento_Run sin excepción no controlada, THE Bot_Saneamiento SHALL salir con código `0`.
9. IF cualquier Saneamiento_Task lanza una excepción no controlada durante una Saneamiento_Run, THEN THE Bot_Saneamiento SHALL salir con código `1` y SHALL emitir un Runtime_Event `kind="saneamiento_run_failed"` con `severity="error"` antes de salir.

### Requirement 2: Aislamiento del Bot_Principal

**User Story:** Como operador, quiero que el Bot_Saneamiento corra como proceso independiente, para que un error de saneamiento nunca tumbe el orchestrator.

#### Acceptance Criteria

1. THE Bot_Saneamiento SHALL ejecutarse como proceso oneshot que termina al completar la Saneamiento_Run.
2. THE Bot_Saneamiento SHALL abrir la DB con la misma función de conexión que usa el Bot_Principal y SHALL cerrar la conexión antes de salir.
3. THE Bot_Saneamiento SHALL NOT importar ni instanciar el orchestrator, los hunters, el dispatcher, el watchdog ni el MCP server del Bot_Principal.
4. WHILE el Bot_Saneamiento está ejecutándose, THE Bot_Principal SHALL poder seguir corriendo en otro proceso sobre la misma DB sin que el Bot_Saneamiento adquiera locks exclusivos prolongados.
5. WHEN el Bot_Saneamiento detecta que el archivo `data/mcp_serve.lock` existe, THE Bot_Saneamiento SHALL continuar la Saneamiento_Run sin abortar y SHALL registrar la concurrencia en el Saneamiento_Report.
6. THE Bot_Saneamiento SHALL escribir su lock propio en `data/saneamiento.lock` durante la Saneamiento_Run para evitar que dos invocaciones concurrentes mutuen el outbox al mismo tiempo.
7. IF el Bot_Saneamiento encuentra un `data/saneamiento.lock` válido al arrancar, THEN THE Bot_Saneamiento SHALL abortar con código `2` y SHALL imprimir el `pid` y `started_at` registrados en el lock sin modificar la DB.

### Requirement 3: Garantía de Dry_Run

**User Story:** Como operador, quiero ejecutar el Bot_Saneamiento sin riesgo por defecto, para inspeccionar qué se haría antes de aplicar cambios.

#### Acceptance Criteria

1. WHILE la Saneamiento_Run está en Dry_Run, THE Bot_Saneamiento SHALL NOT ejecutar `INSERT`, `UPDATE` ni `DELETE` sobre las tablas `outbox`, `published_messages`, `offers`, `products`, `frontier`, `runtime_events` ni ninguna otra tabla persistente.
2. WHILE la Saneamiento_Run está en Dry_Run, THE Bot_Saneamiento SHALL listar por cada Saneamiento_Task la cantidad de candidatos identificados y un máximo de 30 ids de muestra.
3. WHEN una Saneamiento_Run en Dry_Run termina sin error, THE Bot_Saneamiento SHALL imprimir en stdout la línea literal `DRY-RUN: usa --apply para escribir cambios` antes del código de salida `0`.
4. WHILE la Saneamiento_Run está en Apply_Mode, THE Bot_Saneamiento SHALL ejecutar las mutaciones definidas por cada Saneamiento_Task del Requirement 4 dentro de transacciones explícitas commiteadas al final de cada Saneamiento_Task.
5. WHEN una Saneamiento_Task en Apply_Mode falla a mitad de su transacción, THE Bot_Saneamiento SHALL hacer rollback de esa transacción y SHALL registrar la falla en el Saneamiento_Report sin abortar el resto de Saneamiento_Task posteriores.

### Requirement 4: Saneamiento_Task con paridad a los scripts existentes

**User Story:** Como operador, quiero las mismas reglas de limpieza que usan los scripts `_vps_cleanup_*.py` actuales, para retirar esos scripts sin perder cobertura.

#### Acceptance Criteria

1. THE Bot_Saneamiento SHALL ejecutar las Saneamiento_Task en este orden cuando `--task=all`: `outbox-duplicates`, `outbox-missing-prev-price`, `outbox-bad-discounts`, `outbox-stale`.
2. WHEN la Saneamiento_Task `outbox-stale` se ejecuta en Apply_Mode, THE Bot_Saneamiento SHALL marcar como `state='discarded'` cada Stale_Outbox_Item y SHALL escribir el `last_attempt_at` con el timestamp UTC actual en formato ISO-8601 con sufijo `Z`.
3. WHEN la Saneamiento_Task `outbox-duplicates` se ejecuta en Apply_Mode, THE Bot_Saneamiento SHALL marcar como `state='discarded'` cada Published_Duplicate_Outbox_Item y SHALL escribir el `last_attempt_at` con el timestamp UTC actual en formato ISO-8601 con sufijo `Z`.
4. WHEN la Saneamiento_Task `outbox-bad-discounts` se ejecuta en Apply_Mode, THE Bot_Saneamiento SHALL marcar como `state='discarded'` cada Bad_Discount_Outbox_Item y SHALL escribir el `last_attempt_at` con el timestamp UTC actual en formato ISO-8601 con sufijo `Z`.
5. WHEN la Saneamiento_Task `outbox-missing-prev-price` se ejecuta en Apply_Mode, THE Bot_Saneamiento SHALL marcar como `state='discarded'` cada Missing_Prev_Price_Outbox_Item y SHALL NO escribir `last_attempt_at` para preservar el comportamiento de `cleanup_outbox_missing_prev_price.py`.
6. THE Bot_Saneamiento SHALL excluir de la Saneamiento_Task `outbox-missing-prev-price` cualquier `outbox` row cuyo `offers.classification` sea exactamente `price_error_confirmed`.
7. THE Bot_Saneamiento SHALL aplicar el filtro `--marketplace` joineando `outbox` con `offers` y `products` y comparando `products.marketplace` con el valor solicitado en minúsculas.
8. WHEN una Saneamiento_Task se ejecuta en Apply_Mode con cero candidatos, THE Bot_Saneamiento SHALL completar la Saneamiento_Task sin emitir mutaciones y SHALL registrar `affected=0` en el Saneamiento_Report.

### Requirement 5: Idempotencia y reglas de no-borrado

**User Story:** Como operador, quiero que ejecutar el Bot_Saneamiento dos veces seguidas no cambie el resultado, para correrlo en cron sin temor a daños acumulativos.

#### Acceptance Criteria

1. WHEN una Saneamiento_Run en Apply_Mode termina y el Bot_Saneamiento se invoca de inmediato otra vez con los mismos argumentos, THE Bot_Saneamiento SHALL reportar `affected=0` en cada Saneamiento_Task incluida en la segunda Saneamiento_Run.
2. THE Bot_Saneamiento SHALL operar exclusivamente actualizando la columna `state` de filas en la tabla `outbox`, sin ejecutar `DELETE` sobre `outbox` ni sobre ninguna otra tabla persistente.
3. THE Bot_Saneamiento SHALL preservar todos los campos de `outbox.message_payload_json` sin alterarlos durante cualquier Saneamiento_Task.
4. THE Bot_Saneamiento SHALL preservar las filas de `published_messages` sin alterar ningún campo durante cualquier Saneamiento_Task.

### Requirement 6: Saneamiento_Report y auditoría

**User Story:** Como operador, quiero un reporte estructurado y un registro en la DB de cada Saneamiento_Run, para auditar después qué pasó.

#### Acceptance Criteria

1. WHEN una Saneamiento_Run termina en cualquier modo, THE Bot_Saneamiento SHALL imprimir en stdout un Saneamiento_Report con una sección por Saneamiento_Task que incluye `task`, `mode`, `inspected`, `affected`, `marketplace_filter` y hasta 30 `ids_sample`.
2. WHEN una Saneamiento_Run termina en Apply_Mode, THE Bot_Saneamiento SHALL emitir por cada Saneamiento_Task con `affected > 0` un Runtime_Event con `kind="saneamiento_task_applied"`, `severity="info"` y `payload={"task": <nombre>, "affected": <n>, "marketplace_filter": <valor>, "ids_sample": <lista hasta 20 ids>}`.
3. WHEN una Saneamiento_Run termina en Dry_Run, THE Bot_Saneamiento SHALL emitir un único Runtime_Event con `kind="saneamiento_run_dry_run"`, `severity="info"` y `payload` que resume `inspected` por Saneamiento_Task.
4. THE Bot_Saneamiento SHALL preservar el evento legado `kind="outbox_cleanup_missing_prev_price"` cuando la Saneamiento_Task `outbox-missing-prev-price` corre en Apply_Mode con `affected > 0`, para no romper alarmas o tableros que ya consultan ese kind.
5. WHEN una Saneamiento_Task se salta por cero candidatos, THE Bot_Saneamiento SHALL NO emitir Runtime_Event para esa Saneamiento_Task.

### Requirement 7: Despliegue como servicio systemd separado

**User Story:** Como operador, quiero que el Bot_Saneamiento corra periódicamente vía systemd sin colisionar con el `ofertas-hunter-maintenance.timer` existente, para que el VPS quede limpio sin intervención manual.

#### Acceptance Criteria

1. THE Bot_Saneamiento SHALL incluir como entregables `deploy/systemd/ofertas-hunter-saneamiento.service` y `deploy/systemd/ofertas-hunter-saneamiento.timer`.
2. THE archivo `ofertas-hunter-saneamiento.service` SHALL declarar `Type=oneshot` y SHALL invocar como `ExecStart` la línea `__PROJECT_ROOT__/.venv/bin/python -m ofertas_hunter saneamiento --apply --task all`.
3. THE archivo `ofertas-hunter-saneamiento.service` SHALL aplicar el mismo hardening que `ofertas-hunter-maintenance.service`: `User=__SERVICE_USER__`, `WorkingDirectory=__PROJECT_ROOT__`, `EnvironmentFile=__PROJECT_ROOT__/.env`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=__PROJECT_ROOT__/data __PROJECT_ROOT__/logs`, `StandardOutput=journal`, `StandardError=journal`.
4. THE archivo `ofertas-hunter-saneamiento.timer` SHALL declarar `OnUnitActiveSec=6h`, `Persistent=true` y `OnBootSec` con un valor distinto al usado por `ofertas-hunter-maintenance.timer` para escalonar las dos tareas.
5. THE archivo `ofertas-hunter-saneamiento.timer` SHALL declarar `WantedBy=timers.target` en su sección `[Install]`.
6. THE Bot_Saneamiento SHALL incluir el `SyslogIdentifier=ofertas-hunter-saneamiento` para diferenciarse en `journalctl` de `ofertas-hunter-maintenance`.

### Requirement 8: No regresión sobre el Bot_Principal y los scripts retirados

**User Story:** Como mantenedor, quiero que esta feature no rompa los flujos actuales y deje los scripts viejos como wrappers o eliminados a favor del nuevo entrypoint.

#### Acceptance Criteria

1. THE Bot_Principal SHALL preservar sin cambios los subcomandos `run`, `dispatch`, `mcp-serve`, `compress-memory`, `init-db`, `check-config` y `status`.
2. THE Bot_Principal SHALL preservar sin cambios el unit `deploy/systemd/ofertas-hunter-maintenance.service` y `ofertas-hunter-maintenance.timer` que ejecutan `compress-memory`.
3. THE Bot_Saneamiento SHALL retirar los scripts `scripts/_vps_cleanup_stale.py`, `scripts/_vps_cleanup_duplicates.py`, `scripts/_vps_cleanup_bad_discounts.py` y `scripts/cleanup_outbox_missing_prev_price.py` o SHALL convertirlos en wrappers de una sola línea que invocan `python -m ofertas_hunter saneamiento --task <nombre>` con paridad de flags.
4. WHEN el suite de tests del proyecto se ejecuta tras integrar esta feature, THE Bot_Principal SHALL mantener los tests previamente verdes en estado passing.
5. THE Bot_Saneamiento SHALL incluir tests unitarios por cada Saneamiento_Task que cubren: caso vacío, caso con candidatos en Dry_Run sin mutaciones, caso con candidatos en Apply_Mode con mutaciones, idempotencia (segunda ejecución con `affected=0`) y filtro `--marketplace`.
6. WHEN se recolectan diagnostics tras integrar esta feature, THE Bot_Saneamiento SHALL reportar cero diagnostics nuevos en los archivos modificados.
