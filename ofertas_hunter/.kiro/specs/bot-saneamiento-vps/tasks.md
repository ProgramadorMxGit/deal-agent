# Implementation Plan: bot-saneamiento-vps

## Overview

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

Implementación en Python (paridad estricta con `scripts/_vps_cleanup_*.py` y `scripts/cleanup_outbox_missing_prev_price.py`). Reuso total de `ofertas_hunter.db.connect`, `ofertas_hunter.runtime.events.emit_runtime_event` y `ofertas_hunter.config.get_settings`. TDD por `Saneamiento_Task`: test primero, implementación después. Property-based tests con Hypothesis (≥100 ejemplos por propiedad, clock determinista, strategies compartidas). Sin tocar `orchestrator`, `dispatcher`, `watchdog` ni `mcp.server`.

## Tasks

- [x] 1. Esqueleto del módulo `saneamiento` y wiring del subcomando
  - [x] 1.1 Crear el layout del módulo en `src/ofertas_hunter/saneamiento/`
    - Crear `__init__.py` exportando `SaneamientoRunner`, `BaseSaneamientoTask`, `TaskResult`, `SaneamientoReport`, `SaneamientoLock`, `SaneamientoLockBusy`, `SaneamientoRunArgs`, `cmd_saneamiento`
    - Crear archivos vacíos con docstring de cabecera para `cli.py`, `runner.py`, `lockfile.py`, `report.py`, `time_source.py`
    - Crear `tasks/__init__.py` con `TASK_REGISTRY: dict[str, type[BaseSaneamientoTask]]` y export de `BaseSaneamientoTask`, `CandidateRow`
    - Crear archivos vacíos con docstring para `tasks/base.py`, `tasks/outbox_duplicates.py`, `tasks/outbox_missing_prev_price.py`, `tasks/outbox_bad_discounts.py`, `tasks/outbox_stale.py`
    - _Requirements: 2.3_

  - [x] 1.2 Implementar `time_source.py`
    - `now_utc()` devuelve `datetime.now(timezone.utc)`
    - `now_utc_iso(clock)` devuelve `clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")`
    - `cutoff_iso(clock, hours)` devuelve `(clock() - timedelta(hours=hours)).isoformat(...) + 'Z'`
    - _Requirements: 4.2, 4.3, 4.4_

  - [x] 1.3 Wirear el subcomando `saneamiento` en `src/ofertas_hunter/__main__.py`
    - Añadir `sub.add_parser("saneamiento", ...)` con `--task` (`outbox-stale`, `outbox-duplicates`, `outbox-bad-discounts`, `outbox-missing-prev-price`, `all`; default `all`), `--apply` (store_true), `--marketplace` (`amazon`, `mercadolibre`, `all`; default `all`)
    - En el dispatcher de subcomandos, importar `from ofertas_hunter.saneamiento.cli import cmd_saneamiento` y devolver su exit code
    - No importar orchestrator/dispatcher/hunters/watchdog/MCP server desde la rama del subcomando
    - _Requirements: 1.1, 1.2, 1.4, 1.6, 8.1_

  - [ ]* 1.4 Test unitario del wiring de CLI
    - `tests/unit/saneamiento/test_cli_wiring.py`: invocar `python -m ofertas_hunter saneamiento --help` por subprocess y verificar que aparecen `--task`, `--apply`, `--marketplace`
    - Verificar choices exactos vía parser introspection
    - _Requirements: 1.1, 1.2, 1.4, 1.6_

- [x] 2. Implementar `SaneamientoLock` con detección de pid muerto reusando lógica de `mcp/lockfile.py`
  - [x] 2.1 Tests unitarios para `SaneamientoLock` (TDD primero)
    - `tests/unit/saneamiento/test_lockfile.py`: caso path libre → `acquire()` escribe lock con `pid`, `scope="saneamiento"`, `started_at`
    - Caso lock vivo de otro pid → `acquire()` levanta `SaneamientoLockBusy` con `holder` (pid, started_at)
    - Caso lock huérfano (pid muerto) → `acquire()` lo sobrescribe sin error
    - Caso `mcp_serve.lock` presente y vivo → `acquire().mcp_serve_active is True` y NO aborta
    - Caso `mcp_serve.lock` presente y huérfano → `acquire().mcp_serve_active is False` y NO toca el archivo
    - `release()` borra el archivo y es idempotente si ya no existe
    - _Requirements: 2.5, 2.6, 2.7_

  - [x] 2.2 Implementar `lockfile.py`
    - `SaneamientoLock(path, mcp_lock_path)` con `acquire()` / `release()`
    - Reusar la lógica de detección de pid vivo de `src/ofertas_hunter/mcp/lockfile.py` (importar el helper si es público o duplicar la función `_pid_is_alive` con un comentario `# paridad con mcp.lockfile`)
    - Escribir JSON UTF-8 `{"pid": os.getpid(), "scope": "saneamiento", "started_at": "<now>Z"}`
    - Devolver `LockAcquisition(mcp_serve_active, holder)`; `SaneamientoLockBusy.holder` con `pid` y `started_at`
    - _Requirements: 2.5, 2.6, 2.7_

- [x] 3. Implementar `BaseSaneamientoTask`, `CandidateRow`, registry y helper `marketplace_clause`
  - [x] 3.1 Tests unitarios para `BaseSaneamientoTask` y helpers (TDD primero)
    - `tests/unit/saneamiento/test_base_task.py`: `marketplace_clause("all")` → `("", ())`; `marketplace_clause("amazon")` → `(" AND LOWER(p.marketplace) = ? ", ("amazon",))`; idem `mercadolibre`
    - `BaseSaneamientoTask.apply` por defecto: con `writes_last_attempt_at=True` ejecuta `UPDATE outbox SET state='discarded', last_attempt_at=:now WHERE id=:id AND state='pending'`; con `writes_last_attempt_at=False` ejecuta el UPDATE sin `last_attempt_at`
    - Devuelve `rowcount` correcto y respeta `state='pending'` (no toca filas ya `discarded` o `sent`)
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 5.2, 5.3_

  - [x] 3.2 Implementar `tasks/base.py`
    - `BaseSaneamientoTask` (ABC) con `name: ClassVar[str]`, `writes_last_attempt_at: ClassVar[bool] = True`, `select_candidates(...)` abstracto, `apply(...)` con SQL UPDATE compartido
    - `CandidateRow` (frozen dataclass): `outbox_id`, `marketplace`, `title`, `extra: dict`
    - `marketplace_clause(marketplace) -> (sql_fragment, params)` con la lógica del design
    - _Requirements: 4.7, 5.2, 5.3_

  - [x] 3.3 Registrar tasks en `tasks/__init__.py`
    - `TASK_REGISTRY` ordenado: `outbox-duplicates`, `outbox-missing-prev-price`, `outbox-bad-discounts`, `outbox-stale`
    - _Requirements: 1.3, 4.1_

- [x] 4. Implementar `OutboxDuplicatesTask` (TDD)
  - [x] 4.1 Tests unitarios (los 5 casos requeridos)
    - `tests/unit/saneamiento/test_outbox_duplicates.py` usando fixture de DB en memoria con schema real (reusar el helper de `tests/conftest.py` o crear `tests/fixtures/saneamiento_db.py`)
    - Caso vacío: `inspected=0`, `affected=0`
    - Caso Dry_Run con candidatos: `inspected>0`, `affected=0`, snapshot de tablas idéntico antes/después
    - Caso Apply_Mode con candidatos: filas afectadas pasan a `state='discarded'` y `last_attempt_at` actualizado al `now_iso` inyectado
    - Caso idempotencia: segunda ejecución → `affected=0`
    - Caso `--marketplace=amazon`: outbox de `mercadolibre` no se afecta
    - Verificar que la dedup por `outbox.id` ocurre cuando un row matchea por `item_id` y `asin` simultáneamente
    - _Requirements: 4.3, 4.7, 4.8, 5.1, 5.2, 5.3, 5.4, 8.5_

  - [x] 4.2 Implementar `tasks/outbox_duplicates.py`
    - `OutboxDuplicatesTask(BaseSaneamientoTask)` con `name="outbox-duplicates"`, `writes_last_attempt_at=True`
    - `select_candidates`: paso A (SELECT `item_id`, `asin` de `published_messages` con `success=1` y `sent_at >= cutoff_48h`), paso B (SELECT outbox pending duplicado por cada `iid` no nulo joineando offers/products con `marketplace_clause`), dedup por `outbox.id`
    - Cutoff `:cutoff_48h = cutoff_iso(clock, 48)`
    - _Requirements: 4.3, 4.7_

- [x] 5. Implementar `OutboxMissingPrevPriceTask` (TDD)
  - [x] 5.1 Tests unitarios (los 5 casos requeridos)
    - `tests/unit/saneamiento/test_outbox_missing_prev_price.py`
    - Caso vacío, Dry_Run con candidatos, Apply_Mode (verificar que `last_attempt_at` NO se modifica), idempotencia, filtro `--marketplace=mercadolibre`
    - Caso exclusión: row con `offers.classification='price_error_confirmed'` (case-insensitive) NUNCA aparece como candidato
    - Verificar truthiness: `previous_price=None`, `previous_price=0`, `discount_percent=None`, `discount_percent=0`, `discount_percent=""` → todos cuentan como faltantes
    - Verificar que un row con `previous_price=10.0` y `discount_percent=20.0` NO es candidato
    - _Requirements: 4.5, 4.6, 4.7, 4.8, 5.1, 5.2, 5.3, 5.4, 8.5_

  - [x] 5.2 Implementar `tasks/outbox_missing_prev_price.py`
    - `OutboxMissingPrevPriceTask(BaseSaneamientoTask)` con `name="outbox-missing-prev-price"`, `writes_last_attempt_at=False`
    - SELECT con join `offers/products` y `marketplace_clause`; filtrado en Python por classification y truthiness
    - _Requirements: 4.5, 4.6, 4.7_

- [x] 6. Implementar `OutboxBadDiscountsTask` (TDD)
  - [x] 6.1 Tests unitarios (los 5 casos requeridos)
    - `tests/unit/saneamiento/test_outbox_bad_discounts.py`
    - Caso vacío, Dry_Run, Apply_Mode (verificar `last_attempt_at` actualizado), idempotencia, `--marketplace=amazon`
    - Caso `prev <= cur`: candidato
    - Caso `prev <= 0` o `cur <= 0`: candidato
    - Caso `abs(real - disc) > 3.0`: candidato (incluir caso límite `> 3.0` y `<= 3.0`)
    - Caso fila con cualquier campo `None` (`current_price`, `previous_price` o `discount_percent`): NO candidato (paridad con script)
    - _Requirements: 4.4, 4.7, 4.8, 5.1, 5.2, 5.3, 5.4, 8.5_

  - [x] 6.2 Implementar `tasks/outbox_bad_discounts.py`
    - `OutboxBadDiscountsTask(BaseSaneamientoTask)` con `name="outbox-bad-discounts"`, `writes_last_attempt_at=True`
    - SELECT con `json_extract` de `current_price`, `previous_price`, `discount_percent`; filtrado en Python con `real = (prev - cur) / prev * 100.0` y `abs(real - disc) > 3.0`
    - _Requirements: 4.4, 4.7_

- [x] 7. Implementar `OutboxStaleTask` (TDD)
  - [x] 7.1 Tests unitarios (los 5 casos requeridos)
    - `tests/unit/saneamiento/test_outbox_stale.py`
    - Caso vacío, Dry_Run, Apply_Mode (verificar `last_attempt_at` actualizado), idempotencia, `--marketplace=mercadolibre`
    - Caso `enqueued_at` justo en el cutoff de 4h (no candidato), 5min antes (candidato), 5min después (no candidato)
    - Caso `type != 'normal'`: NO candidato
    - Caso `previous_price IS NULL` en payload JSON: NO candidato
    - _Requirements: 4.2, 4.7, 4.8, 5.1, 5.2, 5.3, 5.4, 8.5_

  - [x] 7.2 Implementar `tasks/outbox_stale.py`
    - `OutboxStaleTask(BaseSaneamientoTask)` con `name="outbox-stale"`, `writes_last_attempt_at=True`
    - SELECT con `o.state='pending'`, `o.type='normal'`, `o.enqueued_at < :cutoff_4h`, `json_extract(payload, '$.previous_price') IS NOT NULL`, joins offers/products con `marketplace_clause`
    - Cutoff `:cutoff_4h = cutoff_iso(clock, 4)`
    - _Requirements: 4.2, 4.7_

- [x] 8. Implementar `SaneamientoReport` (`report.py`)
  - [x] 8.1 Tests unitarios para report
    - `tests/unit/saneamiento/test_report.py`
    - `TaskResult` validations: `affected <= inspected`, `len(ids_sample) <= 30`, `affected=0` cuando `mode="dry-run"`
    - `SaneamientoReport.total_inspected`, `total_affected`, `has_errors` calculan correctamente
    - Render legible: incluye una línea por task con `inspected=`, `affected=`, `ids_sample=...`
    - Render JSON: una sola línea prefijada con `JSON: ` y parseable a dict con todos los campos del design
    - En Dry_Run el render imprime al final exactamente `DRY-RUN: usa --apply para escribir cambios`
    - _Requirements: 3.2, 3.3, 6.1_

  - [x] 8.2 Implementar `report.py`
    - Frozen dataclasses `TaskResult` y `SaneamientoReport` con los campos del design
    - `render_human(report) -> str` y `render_json_line(report) -> str`
    - `print_to_stdout(report)` que imprime ambos y, si `mode == "dry-run"`, añade la línea literal final
    - _Requirements: 3.2, 3.3, 6.1_

- [x] 9. Implementar `SaneamientoRunner` (`runner.py`)
  - [x] 9.1 Tests unitarios del runner
    - `tests/unit/saneamiento/test_runner.py` con DB en memoria y registry mockeable
    - `--task=all` ejecuta las 4 tasks en el orden canónico (`outbox-duplicates`, `outbox-missing-prev-price`, `outbox-bad-discounts`, `outbox-stale`)
    - `--task=outbox-stale` ejecuta solo esa task
    - Apply_Mode usa transacción explícita por task (mock de `conn.execute("BEGIN IMMEDIATE")` / `COMMIT`)
    - Si una task lanza excepción en `apply`, se hace `ROLLBACK`, `TaskResult.error` se rellena, las tasks posteriores siguen ejecutando
    - Apply_Mode con `affected>0` emite `runtime_event kind="saneamiento_task_applied"` por task afectada
    - `outbox-missing-prev-price` con `affected>0` emite ADEMÁS `runtime_event kind="outbox_cleanup_missing_prev_price"` (legacy)
    - Dry_Run emite exactamente un `runtime_event kind="saneamiento_run_dry_run"` y cero `saneamiento_task_applied`
    - Tasks con `affected=0` NO emiten runtime_event
    - Excepción no controlada del orquestador → emite `kind="saneamiento_run_failed"` severity `"error"` con `tasks_completed`, `tasks_pending`, `error_type`, `error_message`
    - _Requirements: 1.3, 3.4, 3.5, 4.1, 6.2, 6.3, 6.4, 6.5, 1.9_

  - [x] 9.2 Implementar `runner.py`
    - `SaneamientoRunArgs` (frozen dataclass) con `task`, `apply_mode`, `marketplace`
    - `SaneamientoRunner(conn, args, *, clock, registry, emit_event=emit_runtime_event)` con `run() -> SaneamientoReport`
    - Orden canónico desde `TASK_REGISTRY` o subset según `args.task`
    - Por task: `select_candidates`; en Apply_Mode con candidatos → `BEGIN IMMEDIATE`/`apply`/`COMMIT` con try/except → `ROLLBACK` y `TaskResult.error`
    - Eventos según matriz del design (apply, dry-run, legacy missing-prev-price)
    - Captura de excepción global del orquestador → `saneamiento_run_failed` y re-raise para que `cli` traduzca a exit 1
    - _Requirements: 1.3, 3.4, 3.5, 4.1, 4.8, 6.2, 6.3, 6.4, 6.5_

- [x] 10. Implementar `cli.py` (entrypoint del subcomando)
  - [x] 10.1 Tests unitarios del entrypoint
    - `tests/unit/saneamiento/test_cli.py`
    - Happy path Dry_Run → exit 0, stdout incluye render del report y la línea literal `DRY-RUN: usa --apply para escribir cambios`
    - Happy path Apply_Mode → exit 0
    - Lock vivo de otro pid → exit 2, stdout imprime `pid` y `started_at` del holder, NO toca DB
    - Excepción inyectada (mock del runner) → exit 1, evento `saneamiento_run_failed` emitido
    - `connect()` falla → exit 1, no se escribe lock
    - Convivencia con `mcp_serve.lock` vivo → la run completa, `report.mcp_serve_active is True`
    - _Requirements: 1.8, 1.9, 2.5, 2.7, 3.3_

  - [x] 10.2 Implementar `cli.py`
    - `cmd_saneamiento(args) -> int`: parsea `args` a `SaneamientoRunArgs`, abre `db.connect`, intenta `SaneamientoLock.acquire()` (try/except `SaneamientoLockBusy` → exit 2 con stdout del holder, sin tocar DB), instancia `SaneamientoRunner` y llama `.run()` (try/except global → exit 1 con `saneamiento_run_failed`), imprime el report, `release()` el lock, cierra conn, devuelve 0
    - _Requirements: 1.8, 1.9, 2.1, 2.2, 2.5, 2.6, 2.7, 3.3_

- [x] 11. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 12. Property-based tests (Hypothesis, ≥100 ejemplos por property, clock determinista)
  - [ ]* 12.1 Strategies compartidas en `tests/property/saneamiento/strategies.py`
    - `db_states_st`: `@composite` que crea `:memory:` con schema real e inserta filas aleatorias en `products`, `offers`, `outbox`, `published_messages` cubriendo: marketplace amazon/mercadolibre, classifications mixtos (incluyendo `price_error_confirmed` case-variants), state outbox ∈ {pending, discarded, sent}, payload con/sin `previous_price`/`discount_percent`/`current_price`, `enqueued_at` distribuido a ambos lados del cutoff de 4h, `published_messages` con `success` ∈ {0,1} y `sent_at` dentro/fuera del cutoff de 48h
    - `marketplace_st`, `task_choice_st` (sampled_from)
    - Helper `take_snapshot(conn) -> dict` y `assert_snapshot_equal(a, b)` para fila-a-fila en `outbox`, `published_messages`, `offers`, `products`, `frontier`, `runtime_events`
    - Clock fijo: `FIXED_CLOCK = lambda: datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)`
    - _Requirements: 4.6, 4.7, 5.1, 5.2, 5.3, 5.4_

  - [ ]* 12.2 Property test 1 — Task execution order
    - **Property 1: Task execution order**
    - **Validates: Requirements 1.3, 4.1**
    - `tests/property/saneamiento/test_property_01_task_order.py`
    - `@given(db_states_st, marketplace_st)` con `@settings(max_examples=100)`; `--task=all`; assert `[t.task for t in report.tasks] == ["outbox-duplicates", "outbox-missing-prev-price", "outbox-bad-discounts", "outbox-stale"]`

  - [ ]* 12.3 Property test 2 — Marketplace filter monotonicity
    - **Property 2: Marketplace filter monotonicity**
    - **Validates: Requirements 1.6, 1.7, 4.7**
    - `tests/property/saneamiento/test_property_02_marketplace_filter.py`
    - Para cada task y `mkt ∈ {amazon, mercadolibre}`: `set(ids_mkt) ⊆ set(ids_all)` y todos los `outbox_id` de `ids_mkt` cumplen el join con `LOWER(products.marketplace) = mkt`

  - [ ]* 12.4 Property test 3 — Dry-run is read-only
    - **Property 3: Dry-run is read-only**
    - **Validates: Requirements 3.1, 1.5**
    - `tests/property/saneamiento/test_property_03_dry_run_readonly.py`
    - `snapshot_before = take_snapshot(conn); run(apply=False); assert_snapshot_equal(snapshot_before, take_snapshot(conn))` para todas las tablas

  - [ ]* 12.5 Property test 4 — No-delete invariant
    - **Property 4: No-delete invariant**
    - **Validates: Requirements 5.2**
    - `tests/property/saneamiento/test_property_04_no_delete.py`
    - Wrappear `conn.execute`/`executemany` con un spy que captura todo SQL emitido durante la run; assert `re.search(r"\\bDELETE\\b", sql, re.I)` no matchea ninguno; assert `count(*)` por tabla persistente NO disminuye

  - [ ]* 12.6 Property test 5 — Preservation invariants (payload + published_messages)
    - **Property 5: Preservation invariants**
    - **Validates: Requirements 5.3, 5.4**
    - `tests/property/saneamiento/test_property_05_preservation.py`
    - Pre/post: `outbox.message_payload_json` byte-idéntico para toda fila pre-existente; `published_messages` fila-a-fila idéntica

  - [ ]* 12.7 Property test 6 — Task effect shape
    - **Property 6: Task effect shape**
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.8**
    - `tests/property/saneamiento/test_property_06_task_effect_shape.py`
    - Para cada `outbox_id ∈ TaskResult.ids_sample` post-Apply: `state='discarded'`; `last_attempt_at` actualizado al `now_iso` iff `task != outbox-missing-prev-price`; filas no candidatas no cambian

  - [ ]* 12.8 Property test 7 — Idempotence
    - **Property 7: Idempotence**
    - **Validates: Requirements 5.1**
    - `tests/property/saneamiento/test_property_07_idempotence.py`
    - Ejecutar Apply_Mode dos veces consecutivas con args idénticos; assert segundo report tiene `affected=0` en todas las tasks; snapshot de DB tras 1ª y 2ª runs son iguales

  - [ ]* 12.9 Property test 8 — Report shape
    - **Property 8: Report shape**
    - **Validates: Requirements 3.2, 6.1**
    - `tests/property/saneamiento/test_property_08_report_shape.py`
    - Por cada `TaskResult`: `inspected ≥ 0`, `affected ≥ 0`, `affected ≤ inspected`, `affected == 0` si `mode=="dry-run"`, `len(ids_sample) ≤ 30`, cada id corresponde a una fila pre-existente

  - [ ]* 12.10 Property test 9 — Events shape (apply mode)
    - **Property 9: Events shape — apply mode**
    - **Validates: Requirements 6.2, 6.5**
    - `tests/property/saneamiento/test_property_09_events_apply.py`
    - Cardinalidad de `runtime_events` con `kind='saneamiento_task_applied'` == número de tasks con `affected>0`; payload contiene `task`, `affected`, `marketplace_filter`, `ids_sample` (≤20)

  - [ ]* 12.11 Property test 10 — Events shape (dry-run mode)
    - **Property 10: Events shape — dry-run mode**
    - **Validates: Requirements 6.3, 6.5**
    - `tests/property/saneamiento/test_property_10_events_dry_run.py`
    - Exactamente 1 evento `saneamiento_run_dry_run` y 0 `saneamiento_task_applied`; el payload `tasks[*].inspected` matchea `TaskResult.inspected` de cada task

  - [ ]* 12.12 Property test 11 — Legacy event preserved
    - **Property 11: Legacy event preserved**
    - **Validates: Requirements 6.4**
    - `tests/property/saneamiento/test_property_11_legacy_event.py`
    - Cuando `outbox-missing-prev-price` corre en Apply_Mode con `affected>0`: 1 evento `outbox_cleanup_missing_prev_price` severity `info` con `discarded_count=affected`, `marketplace_filter` correcto, `ids_sample ⊆ TaskResult.ids_sample`

  - [ ]* 12.13 Property test 12 — Price-error-confirmed exclusion
    - **Property 12: Price-error-confirmed exclusion**
    - **Validates: Requirements 4.6**
    - `tests/property/saneamiento/test_property_12_price_error_confirmed.py`
    - Para toda fila con `LOWER(offers.classification)='price_error_confirmed'`: nunca aparece en candidatos de `outbox-missing-prev-price` (Dry_Run y Apply_Mode), independientemente del payload

- [ ] 13. Tests de integración de CLI
  - [ ]* 13.1 Help, dry-run literal y exit codes
    - `tests/integration/saneamiento/test_cli_integration.py`
    - `python -m ofertas_hunter saneamiento --help` → exit 0 y output contiene `--task`, `--apply`, `--marketplace`
    - Dry_Run end-to-end con DB sqlite real temporal → exit 0; stdout incluye exactamente la línea `DRY-RUN: usa --apply para escribir cambios`
    - Apply_Mode con DB real → exit 0; tablas mutadas según las tasks
    - Inyectar lock vivo de otro pid (escribir el archivo manualmente) → exit 2 y stdout imprime `pid` y `started_at`
    - Inyectar excepción no controlada vía monkeypatch del runner → exit 1 y `runtime_event saneamiento_run_failed` registrado
    - _Requirements: 1.1, 1.8, 1.9, 2.7, 3.3_

  - [ ]* 13.2 Test de aislamiento de imports
    - `tests/integration/saneamiento/test_cli_isolation.py`
    - Importar `ofertas_hunter.saneamiento` y todos sus submódulos en un subprocess limpio con `sys.modules` snapshot
    - Assert que ninguno de estos módulos aparece como cargado: `ofertas_hunter.orchestrator`, `ofertas_hunter.dispatching.*`, `ofertas_hunter.runtime.watchdog`, `ofertas_hunter.mcp.server`, `ofertas_hunter.agents.*`
    - _Requirements: 2.3_

- [x] 14. Crear unit files de systemd
  - [x] 14.1 Crear `deploy/systemd/ofertas-hunter-saneamiento.service`
    - `[Unit]` con `Description=ofertas_hunter — saneamiento oneshot (housekeeping del outbox)`
    - `[Service]` con `Type=oneshot`, `User=__SERVICE_USER__`, `WorkingDirectory=__PROJECT_ROOT__`, `EnvironmentFile=__PROJECT_ROOT__/.env`, `ExecStart=__PROJECT_ROOT__/.venv/bin/python -m ofertas_hunter saneamiento --apply --task all`
    - Hardening: `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=__PROJECT_ROOT__/data __PROJECT_ROOT__/logs`
    - `StandardOutput=journal`, `StandardError=journal`, `SyslogIdentifier=ofertas-hunter-saneamiento`
    - _Requirements: 7.1, 7.2, 7.3, 7.6_

  - [x] 14.2 Crear `deploy/systemd/ofertas-hunter-saneamiento.timer`
    - `[Unit]` con `Description=ofertas_hunter — saneamiento outbox (cada 6h)`
    - `[Timer]` con `OnBootSec=45min`, `OnUnitActiveSec=6h`, `Persistent=true`, `Unit=ofertas-hunter-saneamiento.service`
    - `[Install]` con `WantedBy=timers.target`
    - _Requirements: 7.1, 7.4, 7.5_

- [ ] 15. Tests de integración de systemd
  - [ ]* 15.1 Verificar directivas exactas del nuevo service y timer
    - `tests/integration/saneamiento/test_systemd_units.py`
    - Parsear `deploy/systemd/ofertas-hunter-saneamiento.service` con `configparser` (modo permisivo); assert presencia y valor exacto de `Type`, `User`, `WorkingDirectory`, `EnvironmentFile`, `ExecStart`, `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`, `ReadWritePaths`, `StandardOutput`, `StandardError`, `SyslogIdentifier`
    - Parsear `deploy/systemd/ofertas-hunter-saneamiento.timer`; assert `OnUnitActiveSec=6h`, `Persistent=true`, `OnBootSec=45min`, `Unit=ofertas-hunter-saneamiento.service`, `WantedBy=timers.target`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [ ]* 15.2 Verificar staggering vs maintenance.timer y snapshot non-regresión
    - `tests/integration/saneamiento/test_systemd_staggering.py`
    - Leer `OnBootSec` de ambos timers; assert son distintos (`saneamiento=45min`, `maintenance=15min`)
    - Snapshot textual de `deploy/systemd/ofertas-hunter-maintenance.service` y `.timer` contra fixture pre-feature en `tests/fixtures/saneamiento/maintenance_service_baseline.txt` y `maintenance_timer_baseline.txt` para garantizar non-regresión
    - _Requirements: 7.4, 8.2_

- [x] 16. Retirar/wrappear scripts legacy
  - [x] 16.1 Convertir los 4 scripts legacy en wrappers de una línea
    - Reemplazar `scripts/_vps_cleanup_stale.py` por wrapper que invoca `python -m ofertas_hunter saneamiento --task outbox-stale` (con `--apply` y `--marketplace` forwardeados desde `sys.argv`) y forwardea el exit code con `sys.exit(...)`
    - Idem `scripts/_vps_cleanup_duplicates.py` → `--task outbox-duplicates`
    - Idem `scripts/_vps_cleanup_bad_discounts.py` → `--task outbox-bad-discounts`
    - Idem `scripts/cleanup_outbox_missing_prev_price.py` → `--task outbox-missing-prev-price`
    - Cada wrapper preserva el shebang `#!/usr/bin/env python3` y un docstring explicando que es un wrapper de paridad
    - _Requirements: 8.3_

  - [ ]* 16.2 Tests de los wrappers
    - `tests/integration/saneamiento/test_legacy_wrappers.py`
    - Cada wrapper, ejecutado por subprocess sin args y con `--apply`, invoca `python -m ofertas_hunter saneamiento --task <nombre>` (verificar via monkeypatch o capturando args con un fake module)
    - Forwardea correctamente el exit code (0, 1, 2)
    - _Requirements: 8.3_

- [ ] 17. Smoke tests de no regresión del Bot_Principal
  - [ ]* 17.1 Smoke de subcomandos legacy
    - `tests/smoke/test_legacy_subcommands.py`
    - `python -m ofertas_hunter <cmd> --help` retorna 0 para `run`, `dispatch`, `mcp-serve`, `compress-memory`, `init-db`, `check-config`, `status`
    - _Requirements: 8.1_

  - [ ]* 17.2 Snapshot de unit files no tocados
    - `tests/smoke/test_maintenance_units_unchanged.py`
    - Comparar contenido de `deploy/systemd/ofertas-hunter-maintenance.service` y `.timer` con fixtures snapshot pre-feature
    - _Requirements: 8.2_

- [x] 18. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 19. Verificación final
  - [x] 19.1 Ejecutar la suite completa de tests
    - `python -m pytest tests/` (con Hypothesis activo); 0 fallos
    - Re-ejecutar específicamente `tests/property/saneamiento/` para confirmar ≥100 ejemplos por property y reproducibilidad con clock determinista
    - _Requirements: 8.4, 8.5_

  - [x] 19.2 Diagnostics zero-regresión
    - Llamar `getDiagnostics` sobre todos los archivos creados/modificados: `src/ofertas_hunter/saneamiento/**/*.py`, `src/ofertas_hunter/__main__.py`, `deploy/systemd/ofertas-hunter-saneamiento.{service,timer}`, los 4 wrappers en `scripts/`, todos los archivos bajo `tests/unit/saneamiento/`, `tests/property/saneamiento/`, `tests/integration/saneamiento/`, `tests/smoke/test_legacy_subcommands.py`, `tests/smoke/test_maintenance_units_unchanged.py`
    - Confirmar zero diagnostics nuevos en el scope del spec
    - _Requirements: 8.6_

## Notes

- Tareas marcadas con `*` son opcionales (tests) y pueden saltarse para un MVP rápido; las tareas core (skeleton, lockfile, base task, las 4 tasks, runner, cli, wiring de `__main__`, systemd units, wrappers legacy, verificación final) NO son opcionales.
- Cada task referencia los requirement clauses específicos que satisface. Las tareas de property tests llevan además el número de propiedad del design.
- Los tests unitarios de cada `Saneamiento_Task` (4.1, 5.1, 6.1, 7.1) son no-opcionales porque el Requirement 8.5 los exige explícitamente como entregable.
- Las property tests (12.x) cubren las 12 Correctness Properties del design; cada una con `@settings(max_examples=100)` y clock fijo `datetime(2026,5,29,12,0,0,tzinfo=timezone.utc)`.
- Los checkpoints (11, 18) son para validar incrementalmente antes de seguir.
- La verificación final (19) consolida la suite y los diagnostics para cerrar el Requirement 8.6.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "14.1", "14.2", "17.1", "17.2"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1", "4.1", "5.1", "6.1", "7.1", "8.1", "9.1", "10.1", "15.1", "15.2"] },
    { "id": 2, "tasks": ["2.2", "3.2", "8.2"] },
    { "id": 3, "tasks": ["4.2", "5.2", "6.2", "7.2"] },
    { "id": 4, "tasks": ["3.3"] },
    { "id": 5, "tasks": ["9.2"] },
    { "id": 6, "tasks": ["10.2"] },
    { "id": 7, "tasks": ["1.3"] },
    { "id": 8, "tasks": ["1.4", "12.1", "16.1"] },
    { "id": 9, "tasks": ["13.1", "13.2", "16.2", "12.2", "12.3", "12.4", "12.5", "12.6", "12.7", "12.8", "12.9", "12.10", "12.11", "12.12", "12.13"] },
    { "id": 10, "tasks": ["19.1"] },
    { "id": 11, "tasks": ["19.2"] }
  ]
}
```
