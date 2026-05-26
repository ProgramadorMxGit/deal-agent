# Implementation Plan: kiro-cli-orchestrator

## Overview

Plan incremental para exponer un MCP server (12 tools + 2 gemelas de submit) que `kiro-cli` puede usar como orquestador externo del bot `ofertas_hunter`, sin tocar scoring/parsing/Hard_Rules. La estrategia es construir de adentro hacia afuera:

1. **Fundamentos** (FASE A): dependencia, lockfile, serializers, audit, safety, context, server. Cada cimiento se aterriza con sus tests y se mergea antes de pasar al siguiente.
2. **Tools** (FASES B–D): read tools sin efectos, action tools que respetan `Schedule_Authority`, quality gate de dos pasos. Cada tool con sus propiedades.
3. **CLI + steering** (FASES E–F): subcomando `mcp-serve` con lockfile bidireccional y la documentación operativa para `kiro-cli`.
4. **Integración + cierre** (FASE G): smoke in-process recorriendo las 14 tools, verificación de los 344 tests previos, cero diagnostics, y `changes.md`.

Reglas globales aplicadas a cada task:

- Cada task tiene **tests obligatorios antes de marcarse done**.
- Tasks pequeñas, ejecutables 1 por 1.
- Mantener los **344 tests existentes verde** en todo momento; cualquier cambio en código existente (formatter, publisher, `__main__`) es aditivo y default-compatible.
- **Cero diagnostics nuevos** en archivos modificados (verificado con `getDiagnostics`).
- Uso de la lista de Correctness Properties del `design.md` (P-1..P-24); cada property es su propia sub-task.

## Tasks

- [x] 1. FASE A — Fundamentos del MCP server

  - [x] 1.1 Añadir dependencia `mcp>=1.0` y smoke import
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\requirements.txt`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\pyproject.toml`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\__init__.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_dependency_smoke.py`
    - Tests específicos a añadir:
      - `test_mcp_module_importable`: `import mcp.server` y `import mcp.server.stdio` sin error.
      - `test_mcp_types_tool_available`: `from mcp.types import Tool` resuelve.
    - Validation: ejecutar `pytest tests/unit/mcp -q` y `pytest -q` (los 344 tests previos siguen verde).
    - _Requirements: R-1.1, R-1.2, R-10.1_

  - [x] 1.2 Crear `mcp/lockfile.py` con `FileLock` y `Conflict`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\__init__.py` (marker vacío inicialmente)
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\lockfile.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_lockfile.py`
    - Implementación:
      - `class FileLock(path, *, scope)` con `acquire()` (escribe pid+scope), `release()`, `is_held()`, excepción `Conflict`.
      - `acquire()` detecta lock vivo (`os.kill(pid, 0)` en Windows: `OpenProcess`) → `Conflict`. Lock huérfano (pid inexistente) → sobrescribe.
    - Tests específicos a añadir:
      - `test_acquire_creates_file_with_pid_and_scope`.
      - `test_acquire_when_holder_alive_raises_conflict`.
      - `test_acquire_when_holder_dead_overwrites_stale_lock`.
      - `test_release_removes_file`.
      - `test_double_release_is_idempotent`.
    - _Requirements: R-8.3_

  - [x] 1.3 Crear `mcp/serializers.py` para modelos JSON-safe
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\serializers.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_serializers.py`
    - Funciones puras:
      - `serialize_outbox_item(item) -> dict`
      - `serialize_offer(row) -> dict`
      - `serialize_runtime_event(row) -> dict`
      - `serialize_schedule_decision(decision) -> dict`
      - `serialize_publish_outcome(outcome) -> dict`
      - `serialize_revalidation(detail) -> dict`
      - Helpers: `_iso(dt)`, `_safe_decimal(value)`.
    - Tests específicos a añadir:
      - `test_serialize_outbox_item_round_trips_payload`.
      - `test_serialize_runtime_event_includes_severity`.
      - `test_iso_returns_z_suffix_for_utc`.
      - `test_serialize_decimal_returned_as_float`.
      - `test_serialize_schedule_decision_carries_next_change_seconds`.
    - _Requirements: R-2.1, R-2.2, R-2.3, R-2.5_

  - [x] 1.4 Crear `mcp/audit.py` (sanitize + audit_before/after/error)
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\audit.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_audit.py`
    - Implementación:
      - `_SECRET_KEYS` set, `_MAX_STRING_LEN = 500`.
      - `sanitize(value)` recursivo (dict/list/str).
      - `audit_before/after/error(ctx, tool, ...)` emiten `runtime_event(kind="mcp_tool_called")`.
    - Tests específicos a añadir:
      - `test_sanitize_redacts_secret_keys` (`cookies`, `api_key`, `authorization`, `token`, `password`).
      - `test_sanitize_truncates_long_strings_to_500_with_suffix`.
      - `test_audit_before_emits_runtime_event_info`.
      - `test_audit_after_emits_runtime_event_info_with_result_summary`.
      - `test_audit_error_emits_runtime_event_with_severity_error_and_exception_class`.
    - _Requirements: R-7.1, R-7.2, R-7.3_

  - [x] 1.5 Crear `mcp/safety.py` con `SkipResult` y las 7 reglas
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\safety.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_safety.py`
    - Implementación de reglas (cada una `async`):
      - `rule_schedule_authority` → SkipResult con `reason ∈ {"hibernating", "warmup"}`.
      - `rule_marketplace_paused` → consume `ctx.pause_state`.
      - `rule_cooldown_normal` → 5 min global.
      - `rule_publishing_safe_mode` → informativo (siempre OK; el publisher ya respeta flags).
      - `rule_image_price_url` → tokens `missing_image_url`, `missing_current_price`, `missing_url`.
      - `rule_ml_affiliate` → token `missing_affiliate_url`.
      - `rule_telegram_to_ml` → token `telegram_to_ml_blocked`.
      - `apply_hard_rules(ctx, spec, args)` itera `spec.safety_rules` y devuelve la primera que rechaza.
    - Tests específicos a añadir (uno por regla, ambos casos OK + Skip):
      - `test_rule_schedule_authority_skips_when_hibernating`.
      - `test_rule_schedule_authority_skips_when_warmup`.
      - `test_rule_marketplace_paused_skips_with_until_iso`.
      - `test_rule_cooldown_normal_skips_when_within_window`.
      - `test_rule_image_price_url_returns_first_missing_field`.
      - `test_rule_ml_affiliate_skips_when_required_and_missing`.
      - `test_rule_telegram_to_ml_blocks_ml_with_telegram_source`.
      - `test_apply_hard_rules_returns_first_skip` (orden estable).
    - _Requirements: R-3.2, R-6.1, R-6.2, R-6.3, R-6.5, R-6.6_

  - [x] 1.6 Crear `mcp/context.py` con `ServerContext` (singletons + locks)
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\context.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_context.py`
    - Implementación:
      - `@dataclass ServerContext` con campos del design (db, settings, scheduler, builder, agentes lazy, locks, pause_state, review_tokens).
      - Getters async lazy (`get_amazon_hunter`, `get_ml_hunter`, `get_dispatcher`, `get_revalidator`) que cachean la instancia.
      - `lock_for(marketplace) -> asyncio.Lock` con dict default.
      - `from_builder(builder, conn)` factory.
      - `aclose()` que cierra browser worker y EvolutionClient.
    - Tests específicos a añadir:
      - `test_get_amazon_hunter_returns_singleton_across_calls`.
      - `test_get_ml_hunter_singleton`.
      - `test_get_dispatcher_singleton`.
      - `test_lock_for_returns_same_lock_per_marketplace`.
      - `test_from_builder_uses_same_scheduler_as_builder`.
    - _Requirements: R-3.6_

  - [x] 1.7 Crear `mcp/server.py` con `MCPServer.dispatch` (sin tools aún) + smoke handshake
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\server.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\__init__.py` (export `ToolSpec`, `ToolRegistry`, `build_tool_registry` que devuelve `{}` por ahora)
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_server_dispatch.py`
    - Implementación:
      - `@dataclass(frozen=True) ToolSpec` con `name, description, input_schema, handler, is_read_only, requires_active_schedule, safety_rules, descriptor()`.
      - `MCPServer(ctx, lockfile, registry=None)` con lazy import de `mcp.server`.
      - `dispatch(name, args)` siguiendo el flujo del design (validación → audit_before → hard rules → handler → audit_after / audit_error).
      - `serve_stdio()` y `shutdown()` definidos pero sin tools aún (registry vacío).
    - Tests específicos a añadir:
      - `test_dispatch_unknown_tool_returns_error`.
      - `test_dispatch_validation_error_emits_audit_error_event`.
      - `test_dispatch_handler_exception_returns_handler_failed_with_class_name`.
      - `test_dispatch_handler_exception_emits_audit_error_event`.
      - `test_dispatch_skip_from_safety_returns_skipped_payload`.
      - `test_dispatch_success_emits_two_audit_events_before_and_after`.
      - `test_register_tools_returns_descriptors_with_additional_properties_false` (smoke con registry de prueba).
    - _Requirements: R-1.2, R-1.3, R-7.1, R-7.3_
    - Properties validadas (parcial): P-20

  - [x] 1.8 Checkpoint A — Ensure all tests pass
    - Ejecutar `pytest -q` y verificar 344+nuevos pasan.
    - Verificar `getDiagnostics` cero en todos los archivos creados de `src/ofertas_hunter/mcp/` y `tests/unit/mcp/`.
    - Si surgen dudas (por ejemplo, comportamiento del lockfile en Windows vs Linux), preguntar al usuario.

- [x] 2. FASE B — Read tools (sin efectos secundarios)

  - [x] 2.1 `build_read_tools` con `get_status` y `get_schedule_mode`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\read_tools.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_read_tools_status.py`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\__init__.py` (importa y compone `build_read_tools`).
    - Implementación:
      - `get_status` devuelve `schedule_mode`, `publishing_enabled`, `publishing_dry_run`, `agents_registered`, `marketplace_paused`, `lock_holder`.
      - `get_schedule_mode` devuelve `mode`, `next_mode`, `next_change_in_seconds`, `local_time`.
      - Ambas con `is_read_only=True`, `safety_rules=()`, `input_schema` con `additionalProperties: false`.
    - Tests específicos a añadir:
      - `test_get_status_returns_schedule_mode_and_flags`.
      - `test_get_status_includes_paused_marketplaces`.
      - `test_get_schedule_mode_returns_next_change_in_seconds_int`.
    - _Requirements: R-2.1, R-2.5_

  - [x] 2.2 `get_outbox` y `get_recent_events`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\read_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_read_tools_outbox.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_read_tools_events.py`
    - Implementación:
      - `get_outbox(limit, type_filter)` con SQL `SELECT ... FROM outbox WHERE (?='any' OR type=?) ORDER BY enqueued_at DESC LIMIT ?`.
      - `get_recent_events(limit, severity)` con SQL análogo sobre `runtime_events`.
    - Tests específicos a añadir:
      - `test_get_outbox_respects_limit`.
      - `test_get_outbox_filters_by_type_when_not_any`.
      - `test_get_outbox_orders_desc_by_enqueued_at`.
      - `test_get_recent_events_respects_severity_filter`.
      - `test_get_recent_events_respects_limit`.
    - Properties (sub-tasks marcadas como tests propios, ver 2.4):
      - Cubre P-3 y P-4 a nivel de unit.
    - _Requirements: R-2.2, R-2.3_

  - [x] 2.3 `get_frontier_stats(marketplace)`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\read_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_read_tools_frontier.py`
    - Implementación:
      - SQL `SELECT kind, COUNT(*) AS n FROM frontier WHERE marketplace=? GROUP BY kind`.
      - Devuelve `{marketplace, by_kind, total}`.
    - Tests específicos a añadir:
      - `test_get_frontier_stats_returns_zero_when_empty`.
      - `test_get_frontier_stats_groups_by_kind`.
      - `test_get_frontier_stats_total_matches_sum_of_by_kind`.
    - _Requirements: R-2.4_

  - [x]* 2.4 Property test "read tools never mutate persistent state"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_read_tools_no_mutation_property.py`
    - **Property 2: Read tools never mutate persistent state**
    - **Validates: Requirements R-2.6**
    - Estrategia: Hypothesis con DB poblada aleatoriamente; tomar `dump_sql_state(conn)` excluyendo filas de auditoría posteriores; invocar secuencias aleatorias de `get_status / get_outbox / get_recent_events / get_frontier_stats / get_schedule_mode`; verificar dumps idénticos.
    - Mínimo 100 iteraciones (`@settings(max_examples=100)`).
    - Tag obligatorio en docstring: `Feature: kiro-cli-orchestrator, Property 2: Read tools never mutate persistent state`.

  - [x]* 2.5 Smoke "handshake anuncia los 5 nombres correctamente"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_handshake_announces_read_tools.py`
    - Tests específicos:
      - `test_list_tools_includes_5_read_names_after_phase_b`.
      - `test_each_read_tool_descriptor_has_additional_properties_false`.
    - _Requirements: R-1.3, R-2.1, R-2.2, R-2.3, R-2.4, R-2.5_

- [x] 3. FASE C — Action tools

  - [x] 3.1 `pause_marketplace` y `unpause_marketplace` con TTL
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\action_tools.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_pause.py`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\__init__.py` (compone `build_action_tools`).
    - Implementación:
      - `pause_marketplace(name, reason, ttl_seconds)` graba `PauseInfo`. Si `ttl_seconds > 0`, programa `loop.call_later`.
      - `unpause_marketplace(name)` cancela timer y limpia.
      - Emite `runtime_event` `mcp_marketplace_paused` / `mcp_marketplace_unpaused`.
      - `safety_rules=()` (control puro; no toca productos).
    - Tests específicos a añadir:
      - `test_pause_marketplace_sets_pause_state_active`.
      - `test_pause_marketplace_with_ttl_clears_after_ttl_with_clock_mock` (usa `loop` controlado).
      - `test_unpause_marketplace_cancels_timer`.
      - `test_pause_marketplace_emits_runtime_event_warning`.
    - _Requirements: R-3.4, R-3.5_

  - [x] 3.2 `discover_seeds` reusando `DiscoveryAgent` cacheado
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\action_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_discover.py`
    - Implementación:
      - `discover_seeds(marketplace, limit)` con `safety_rules=("schedule_authority", "marketplace_paused")`.
      - Bajo `ctx.lock_for(marketplace)`.
      - Llama a `discovery_agent.discover_once(max_per_cycle=min(limit, 10))`.
    - Tests específicos a añadir:
      - `test_discover_seeds_calls_existing_agent_singleton`.
      - `test_discover_seeds_skipped_during_hibernating` (verifica que el agente NO se invoca).
      - `test_discover_seeds_skipped_during_warmup`.
      - `test_discover_seeds_skipped_when_marketplace_paused`.
    - _Requirements: R-3.1, R-3.2, R-3.6_

  - [x] 3.3 `hunt_amazon` y `hunt_mercadolibre` con skip durante hibernating
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\action_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_hunt_amazon.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_hunt_mercadolibre.py`
    - Implementación:
      - `hunt_amazon(limit)` con lock de marketplace, `await ctx.get_amazon_hunter().hunt_from_frontier(max_urls=limit)`, devuelve resumen.
      - `hunt_mercadolibre(limit)` análogo. Si el hunter ML está pausado por `cookie_expiry`, devuelve `{"skipped": true, "reason": "ml_paused_for_login"}`.
    - Tests específicos a añadir:
      - `test_hunt_amazon_calls_existing_agent_singleton`.
      - `test_hunt_amazon_skipped_during_hibernating_does_not_call_agent`.
      - `test_hunt_amazon_skipped_during_warmup`.
      - `test_hunt_mercadolibre_skipped_when_paused_for_login`.
      - `test_hunt_mercadolibre_returns_outcomes_summary`.
    - _Requirements: R-3.1, R-3.2, R-3.6_

  - [x] 3.4 `dispatch_outbox` + property cooldown
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\action_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_dispatch.py`
    - Implementación:
      - `dispatch_outbox(limit)` con `safety_rules=("schedule_authority",)`, bajo `ctx.dispatcher_lock`, llama `dispatcher.tick()` hasta `min(limit, 10)` veces.
      - Devuelve `{success, ticks, results: [serialize_publish_outcome(...)]}`.
    - Tests específicos a añadir:
      - `test_dispatch_outbox_calls_dispatcher_tick_n_times`.
      - `test_dispatch_outbox_skipped_during_warmup`.
      - `test_dispatch_outbox_blocks_normal_within_cooldown` (single example).
      - `test_dispatch_outbox_blocks_ml_without_affiliate`.
      - `test_dispatch_outbox_blocks_telegram_to_ml`.
      - `test_dispatch_outbox_returns_dry_run_in_safe_mode`.
    - _Requirements: R-3.1, R-3.2, R-6.1, R-6.2, R-6.3, R-6.4, R-6.6_

  - [x]* 3.5 Property test "cooldown blocks normal publications within 5 minutes"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_dispatch_cooldown_property.py`
    - **Property 15: Cooldown blocks normal publications within 5 minutes**
    - **Validates: Requirements R-6.1**
    - Hypothesis: timestamps `t0` aleatorios; `delta ∈ [0, whatsapp_cooldown_seconds]`; verifica skip y que `EvolutionClient.send_media` no fue llamado.
    - Mínimo 100 iteraciones.

  - [x] 3.6 `revalidate_offer(outbox_id)` reusando `PlaywrightRevalidator`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\action_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_revalidate.py`
    - Implementación:
      - `revalidate_offer(outbox_id)` con `safety_rules=()`, llama `await ctx.get_revalidator().revalidate_detailed(item)`.
      - Devuelve `serialize_revalidation(detail)`.
    - Tests específicos a añadir:
      - `test_revalidate_offer_returns_classification_and_confidence`.
      - `test_revalidate_offer_returns_outbox_not_found_when_id_missing`.
      - `test_revalidate_offer_reuses_revalidator_singleton`.
    - _Requirements: R-3.3, R-3.6_

  - [x]* 3.7 Property test "action tools reuse singletons (no parallel scrapers)"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_action_tools_singletons_property.py`
    - **Property 8: Action tools reuse existing agents (no parallel scrapers)**
    - **Validates: Requirements R-3.6**
    - Hypothesis: secuencias aleatorias de invocaciones de action tools; verifica `id(ctx.amazon_hunter)`, `id(ctx.ml_hunter)`, `id(ctx.dispatcher)`, `id(ctx.browser)` estables y `is`-iguales a los del builder.
    - Mínimo 100 iteraciones.

  - [x]* 3.8 Property test "Hard_Rules cannot be overridden by client args"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_hard_rules_no_override_property.py`
    - **Property 14: Hard_Rules cannot be overridden by client args**
    - **Validates: Requirements R-6.5**
    - Hypothesis: argumentos válidos + extras aleatorios (`force`, `bypass_schedule`, `override_cooldown`, `override_affiliate`, claves random); verifica `validation_failed` por `additionalProperties: false` y que `apply_hard_rules` produce el mismo resultado bit-equal con/sin extras.
    - Mínimo 100 iteraciones.

  - [x] 3.9 Checkpoint C — Ensure all tests pass
    - Ejecutar `pytest -q`; verificar verde y `getDiagnostics` limpio en `mcp/tools/action_tools.py` y tests asociados.

- [x] 4. FASE D — Quality gate (dos pasos)

  - [x] 4.1 `ReviewSession` + helpers `_load_outbox_item`, `_preview_message`, `_mark_eligible`, `_mark_discarded`, `_apply_rewrite`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\quality_tools.py` (con `ReviewSession`, helpers, sin las tools aún).
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_quality_helpers.py`
    - Implementación:
      - `@dataclass ReviewSession(token, outbox_id, kind, snapshot_payload, created_at, expires_at)`.
      - `_load_outbox_item(db, outbox_id)`.
      - `_preview_message(item)` reutiliza `formatter.format_message(item, dry_run=True)`.
      - `_mark_eligible(db, outbox_id)` → state `pending`, payload sin cambios.
      - `_mark_discarded(db, outbox_id, reason)` → state `discarded` y guarda `discard_reason`.
      - `_apply_rewrite(db, outbox_id, snapshot_payload, new_text)` → actualiza solo `payload["caption_override"]`.
    - Tests específicos a añadir:
      - `test_load_outbox_item_returns_none_when_missing`.
      - `test_preview_message_uses_caption_override_when_present`.
      - `test_mark_eligible_does_not_mutate_payload`.
      - `test_mark_discarded_persists_reason`.
      - `test_apply_rewrite_only_modifies_caption_override`.
      - `test_apply_rewrite_preserves_image_url_url_prices_marketplace_score`.
    - _Requirements: R-4.3, R-4.4, R-4.5_

  - [x] 4.2 Modificar formatter/whatsapp_publisher para respetar `caption_override`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\publishing\formatter.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\publishing\whatsapp_publisher.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\publishing\test_caption_override.py`
    - Implementación: cambio aditivo. Si `payload.get("caption_override")` no es vacío, formatter lo usa como texto del mensaje preservando todo lo demás. Default sin `caption_override`: comportamiento idéntico al previo (verificable con tests existentes).
    - Tests específicos a añadir:
      - `test_formatter_uses_caption_override_when_present`.
      - `test_formatter_falls_back_to_template_when_caption_override_absent`.
      - `test_caption_override_does_not_affect_image_url_or_price`.
      - `test_existing_formatter_tests_still_pass` (smoke: ejecutar `tests/unit/publishing/`).
    - **Crítico**: ejecutar la suite completa `pytest -q` antes de marcar done; los 344 tests deben seguir verde.
    - _Requirements: R-4.5, R-5.2_

  - [x] 4.3 `request_offer_review` y `submit_offer_review`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\quality_tools.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\__init__.py` (compone `build_quality_tools`).
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_quality_tools_review.py`
    - Implementación:
      - `request_offer_review(outbox_id)` con `safety_rules=("image_price_url", "ml_affiliate", "telegram_to_ml")`. Si gate falla → skip; si pasa → crea `ReviewSession`, devuelve `review_token`, `outbox`, `formatted_preview`.
      - `submit_offer_review(outbox_id, review_token, decision, reason?, new_text?)` con `additionalProperties: false`; valida token + outbox_id; despacha por decisión.
    - Tests específicos a añadir:
      - `test_request_offer_review_creates_session_with_token`.
      - `test_request_offer_review_skipped_when_image_missing`.
      - `test_request_offer_review_skipped_when_ml_missing_affiliate`.
      - `test_request_offer_review_skipped_when_telegram_to_ml`.
      - `test_submit_offer_review_approve_marks_eligible`.
      - `test_submit_offer_review_reject_with_reason_marks_discarded`.
      - `test_submit_offer_review_rewrite_message_updates_caption_only`.
      - `test_submit_offer_review_invalid_token_returns_error`.
      - `test_submit_offer_review_token_mismatch_returns_error`.
      - `test_submit_offer_review_unknown_decision_validation_failed`.
      - `test_submit_offer_review_missing_new_text_for_rewrite_returns_error`.
    - _Requirements: R-4.1, R-4.2, R-4.3, R-4.4, R-4.5, R-4.6_

  - [x] 4.4 `improve_message_copy` y `submit_message_copy`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\tools\quality_tools.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_quality_tools_message_copy.py`
    - Implementación:
      - `improve_message_copy(outbox_id, current_text)` con mismas `safety_rules` que `request_offer_review`. Si gate falla → skip con `missing_<field>`.
      - `submit_message_copy(outbox_id, review_token, new_text)` aplica `_apply_rewrite`.
    - Tests específicos a añadir:
      - `test_improve_message_copy_returns_token_and_payload`.
      - `test_improve_message_copy_refuses_when_image_missing`.
      - `test_improve_message_copy_refuses_when_ml_missing_affiliate`.
      - `test_submit_message_copy_updates_caption_only`.
      - `test_submit_message_copy_preserves_url_price_marketplace`.
    - _Requirements: R-5.1, R-5.2, R-5.3_

  - [x]* 4.5 Property tests del quality gate
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_quality_properties.py`
    - **Property 9: Copy edits preserve material fields**
      - **Validates: Requirements R-4.5, R-5.2**
    - **Property 10: Approve preserves payload exactly**
      - **Validates: Requirements R-4.3**
    - **Property 11: Reject persists discard reason**
      - **Validates: Requirements R-4.4**
    - **Property 12: Invalid review responses are no-ops**
      - **Validates: Requirements R-4.6**
    - Hypothesis: outbox items aleatorios (con/sin gates pasando), payloads aleatorios para `new_text` y `reason`, decisiones inválidas. Mínimo 100 iteraciones por property.
    - Tags obligatorios en docstrings: `Feature: kiro-cli-orchestrator, Property N: <text>`.

  - [x] 4.6 Checkpoint D — Ensure all tests pass
    - Ejecutar `pytest -q`; verificar 344 originales + nuevos verde.
    - `getDiagnostics` limpio en `mcp/tools/quality_tools.py`, `formatter.py`, `whatsapp_publisher.py`.

- [x] 5. FASE E — Subcomando CLI `mcp-serve`

  - [x] 5.1 `cmd_mcp_serve` en `__main__.py` + lockfile bidireccional con `cmd_run`
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\__main__.py`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_cli_mcp_serve.py`
    - Implementación:
      - Subparser `mcp-serve` con flags `--no-lock` y `--log-level`.
      - `cmd_mcp_serve(args)`: `init_db`, `get_settings`, `FileLock(... scope="mcp-serve").acquire()` (a menos que `--no-lock`), construir `OrchestratorConfig` con los mismos defaults que `cmd_run`, `connect()`, `AgentFactoryBuilder`, `ServerContext.from_builder`, `MCPServer.serve_stdio()`. `try/finally` libera lock y cierra conn.
      - `cmd_run` también adquiere `FileLock(scope="run")` al inicio (si no existe ya). Cambio aditivo, los tests existentes deben seguir verde.
    - Tests específicos a añadir:
      - `test_mcp_serve_subparser_is_registered`.
      - `test_cmd_mcp_serve_exits_2_when_lock_held_by_run`.
      - `test_cmd_mcp_serve_with_no_lock_skips_acquire`.
      - `test_cmd_run_acquires_lockfile_with_scope_run` (smoke; reproduce defaults).
      - `test_cmd_run_defaults_unchanged_byte_equal_to_baseline` (compara `OrchestratorConfig` construido vs snapshot capturado al inicio del task).
    - _Requirements: R-1.1, R-1.4, R-8.1, R-8.2, R-8.3_

  - [x]* 5.2 Property test "lockfile bloquea concurrent run + mcp-serve"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_lockfile_concurrency_property.py`
    - **Property 23: Lockfile blocks concurrent run + mcp-serve**
    - **Validates: Requirements R-8.3**
    - Hypothesis: pares de scopes aleatorios `(run|mcp-serve, run|mcp-serve)`, orden aleatorio de acquire/release; verifica exit code 2 cuando hay conflicto y mensaje de stderr con pid+scope; verifica re-acquire exitoso tras release.
    - Mínimo 100 iteraciones.

  - [x]* 5.3 Property test "defaults de cmd_run intactos"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_cmd_run_defaults_property.py`
    - **Property 22: `python -m ofertas_hunter run` defaults are unchanged**
    - **Validates: Requirements R-8.1, R-8.2**
    - Estrategia: Hypothesis genera entornos vacíos vs default; construye `OrchestratorConfig` vía `cmd_run` y verifica byte-equal contra snapshot baseline (`dispatcher_loop_interval`, `amazon_loop_interval`, ..., `hibernate_start`, `active_start`, etc.).
    - Mínimo 100 iteraciones.

  - [x] 5.4 Checkpoint E — Ensure all tests pass
    - Ejecutar `pytest -q`; verificar verde y `getDiagnostics` limpio en `__main__.py`.

- [x] 6. FASE F — Steering + docs operativas

  - [x] 6.1 Steering `.kiro/steering/ofertas-hunter-mcp.md` con scope workspace
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\.kiro\steering\ofertas-hunter-mcp.md`
    - Frontmatter:
      ```yaml
      ---
      inclusion: fileMatch
      fileMatchPattern: "*"
      ---
      ```
    - Secciones requeridas:
      1. Objetivo del bot.
      2. Hard_Rules con tokens (`cooldown_active`, `missing_image_url`, `missing_current_price`, `missing_url`, `missing_affiliate_url`, `hibernating`, `warmup`, `paused`, `telegram_to_ml_blocked`).
      3. Ciclo recomendado.
      4. Cuándo NO insistir.
      5. Interpretación de `runtime_events` (`cookie_expiry`, `captcha`, `agent_paused`, `agent_skipped`, `mcp_tool_called`).
      6. Anti-patterns del legacy `AmazonScrapperIA` (tools por producto, scoring/parsing en LLM, override de Hard_Rules).
    - _Requirements: R-9.1, R-9.2, R-9.3_

  - [x] 6.2 Docs `docs/MCP_CLIENT_SETUP.md`
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\docs\MCP_CLIENT_SETUP.md`
    - Contenido:
      - Fragmento JSON listo para `~/.kiro/settings/mcp.json` (Windows + Linux/macOS).
      - Notas: usar `python.exe` del venv, `cwd` en raíz del repo, `autoApprove` solo las 5 read tools, action y quality requieren confirmación.
      - Sección de troubleshooting básica (lockfile en conflicto, `--no-lock` solo para tests, logs en stderr).
    - _Requirements: R-1.4, R-1.5_

  - [x] 6.3 Test "steering existe con scope workspace y secciones esperadas"
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\unit\mcp\test_steering_workspace_scope.py`
    - **Property 24: Steering file is workspace-scoped and complete**
    - **Validates: Requirements R-9.1, R-9.2, R-9.3**
    - Tests específicos:
      - `test_steering_file_exists`.
      - `test_steering_frontmatter_has_inclusion_filematch_and_pattern_star`.
      - `test_steering_has_required_section_headings` (las 6 secciones).
      - `test_steering_documents_hard_rule_tokens` (verifica que los tokens están literalmente presentes).
      - `test_steering_documents_legacy_anti_patterns` (menciona `AmazonScrapperIA`).
    - _Requirements: R-9.1, R-9.2, R-9.3_

- [x] 7. FASE G — Smoke integración + cierre

  - [x] 7.1 Smoke in-process recorriendo las 14 tools
    - Archivos a crear:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\integration\__init__.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\integration\mcp\__init__.py`
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\tests\integration\mcp\test_mcp_smoke_in_process_client.py`
    - Tests específicos:
      - `test_handshake_announces_14_canonical_tools` (5 read + 7 action + 4 quality).
      - `test_each_descriptor_has_additional_properties_false`.
      - `test_dispatch_each_read_tool_returns_data_shape`.
      - `test_dispatch_each_action_tool_returns_skipped_or_success_shape` (con scheduler en hibernating + ML pausado para forzar skip rápido).
      - `test_dispatch_request_and_submit_offer_review_round_trip`.
      - `test_dispatch_improve_and_submit_message_copy_round_trip`.
      - `test_audit_emits_two_events_per_call_in_runtime_events`.
    - **Properties cubiertas (smoke)**: P-1, P-20, P-21.
    - _Requirements: R-1.2, R-1.3, R-7.1, R-7.2, R-7.3, R-10.3_

  - [x] 7.2 Verificar 344 tests previos siguen verde
    - Ejecutar `pytest -q` con la suite completa.
    - Confirmar `passed >= 344 + nuevos`, `failed == 0`, `errors == 0`.
    - Si falla cualquiera de los 344 originales: detener y diagnosticar root cause antes de avanzar.
    - _Requirements: R-10.1_

  - [x] 7.3 Validar cero diagnostics en archivos modificados
    - Archivos a verificar con `getDiagnostics`:
      - Todo `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\src\ofertas_hunter\mcp\**`.
      - `src\ofertas_hunter\__main__.py`.
      - `src\ofertas_hunter\publishing\formatter.py`.
      - `src\ofertas_hunter\publishing\whatsapp_publisher.py`.
      - Todo `tests\unit\mcp\**` y `tests\integration\mcp\**`.
    - Aceptación: cero diagnostics nuevos. Si hay warnings preexistentes, dejarlos sin tocar a menos que el usuario indique lo contrario.
    - _Requirements: R-10.4_

  - [x] 7.4 Actualizar `changes.md` con la fase MCP completa
    - Archivos a modificar:
      - `c:\Users\yarteaga\Desktop\bot_autonomo_ofert\ofertas_hunter\changes.md`
    - Contenido a añadir:
      - Resumen de la feature (MCP server + 14 tools + steering + docs).
      - Lista de archivos nuevos en `src/ofertas_hunter/mcp/`.
      - Cambios aditivos en `formatter.py`, `whatsapp_publisher.py`, `__main__.py`.
      - Mención explícita de Hard_Rules inviolables y dry-run por defecto.
      - Conteo final de tests `pytest --collect-only -q | tail -1`.
    - _Requirements: R-10.1, R-10.2, R-10.3, R-10.4_

  - [x] 7.5 Checkpoint final — Ensure all tests pass and ask the user if questions arise
    - Ejecutar `pytest -q` una última vez.
    - Confirmar `getDiagnostics` cero en archivos modificados.
    - Reportar al usuario:
      - Conteo de tests previos (debe ser ≥ 344) + nuevos.
      - Lista de archivos creados/modificados.
      - Cualquier deuda detectada.

## Notes

- Tasks marcadas con `*` son property tests opcionales (skippables para MVP rápido), pero recomendadas para validar invariantes universales antes de soltar la feature.
- Cada task referencia requirements (`R-X.Y`) y, cuando aplica, properties (`P-N`).
- Checkpoints (1.8, 3.9, 4.6, 5.4, 7.5) son obligatorios y bloquean el avance hasta que `pytest -q` esté verde.
- Los cambios en `formatter.py`, `whatsapp_publisher.py` y `__main__.py` son **aditivos** y default-compatible; los 344 tests previos deben seguir pasando sin modificaciones a esos tests.
- El servidor MCP solo arranca al ejecutar `python -m ofertas_hunter mcp-serve`. `python -m ofertas_hunter run` es el fallback intacto.
- Estado de las dependencias del DAG: A1→A2→A3→A4→A5→A6→A7 estrictamente secuencial. B/C/D/E/F pueden empezar en paralelo tras A7 (con sus dependencias propias). G espera a B/C/D/E/F.
- Si una property test falla, el orden de remediación es: (1) leer el counterexample, (2) decidir si el bug es en el handler o en el test, (3) reproducir como unit test mínimo, (4) corregir, (5) re-ejecutar la property con `max_examples=200` para confirmar.
- Steering scoped al workspace (`.kiro/steering/ofertas-hunter-mcp.md`) y docs (`docs/MCP_CLIENT_SETUP.md`) son entregables documentales, no código ejecutable; sus tests verifican estructura.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4"] },
    { "id": 3, "tasks": ["1.5", "1.6"] },
    { "id": 4, "tasks": ["1.7"] },
    { "id": 5, "tasks": ["2.1", "3.1", "4.1", "4.2", "6.1", "6.2"] },
    { "id": 6, "tasks": ["2.2", "2.3", "3.2", "3.3", "3.6", "4.3", "5.1", "6.3"] },
    { "id": 7, "tasks": ["2.4", "2.5", "3.4", "4.4", "5.2", "5.3"] },
    { "id": 8, "tasks": ["3.5", "3.7", "3.8", "4.5"] },
    { "id": 9, "tasks": ["7.1"] },
    { "id": 10, "tasks": ["7.2", "7.3", "7.4"] }
  ]
}
```
