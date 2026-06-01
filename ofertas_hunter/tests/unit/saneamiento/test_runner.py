"""Tests unitarios para ``ofertas_hunter.saneamiento.runner``.

Estos tests definen el contrato del ``SaneamientoRunner`` descrito en
``.kiro/specs/bot-saneamiento-vps/design.md`` (sección
``Components and Interfaces`` → ``SaneamientoRunner`` y matriz de
eventos en ``Architecture``). Se escriben antes de la implementación
(TDD-RED): la tarea 9.2 hará que estos tests pasen.

Contrato resumido (ver design.md):

- ``SaneamientoRunArgs`` — frozen dataclass con ``task``,
  ``apply_mode``, ``marketplace``.
- ``SaneamientoRunner(conn, args, *, clock, registry, emit_event)`` con
  ``run() -> SaneamientoReport``.
- Para ``--task=all`` se ejecutan las 4 tasks del registry en orden
  canónico de iteración (``outbox-duplicates`` →
  ``outbox-missing-prev-price`` → ``outbox-bad-discounts`` →
  ``outbox-stale``).
- Para ``--task=<name>`` se ejecuta sólo esa task.
- Por cada task se llama ``select_candidates`` (siempre).
- En Apply_Mode con ``len(candidates) > 0`` el runner abre una
  transacción explícita por task: ``conn.execute("BEGIN IMMEDIATE")``,
  ``task.apply(...)``, ``conn.execute("COMMIT")``. Si ``apply`` lanza
  cualquier excepción → ``conn.execute("ROLLBACK")``,
  ``TaskResult.error = "<ExcType>: <msg>"``, ``affected=0``, y el
  runner continúa con la siguiente task.
- Tras procesar todas las tasks:
    * En Apply_Mode, por cada ``TaskResult`` con ``affected > 0`` se
      emite un ``runtime_event`` ``kind="saneamiento_task_applied"``
      severity ``"info"`` con ``payload = {task, affected,
      marketplace_filter, ids_sample (≤20)}``.
    * Si la task ``outbox-missing-prev-price`` corre en Apply_Mode con
      ``affected > 0``, se emite ADEMÁS el evento legacy
      ``kind="outbox_cleanup_missing_prev_price"`` severity ``"info"``
      con ``payload = {discarded_count, marketplace_filter, ids_sample}``.
    * En Dry_Run se emite exactamente UN evento
      ``kind="saneamiento_run_dry_run"`` severity ``"info"`` con
      ``payload.tasks = [{task, inspected}, ...]``. NO se emite ningún
      ``saneamiento_task_applied``.
    * Tasks con ``affected = 0`` (cualquier modo) NO emiten
      ``saneamiento_task_applied``.
- Si una excepción no controlada escapa al orquestador (p. ej. una
  excepción dentro de ``select_candidates``, fuera del try/except del
  apply), el runner emite ``kind="saneamiento_run_failed"`` severity
  ``"error"`` con ``payload = {marketplace_filter, mode, error_type,
  error_message, tasks_completed, tasks_pending}`` y re-lanza la
  excepción.

Cobertura: Requirements 1.3, 3.4, 3.5, 4.1, 6.2, 6.3, 6.4, 6.5, 1.9.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from ofertas_hunter.saneamiento.runner import (
    SaneamientoRunArgs,
    SaneamientoRunner,
)
from ofertas_hunter.saneamiento.tasks.base import (
    BaseSaneamientoTask,
    CandidateRow,
)


# ---------------------------------------------------------------------------
# Constantes deterministas — paridad con las property tests del spec.
# ---------------------------------------------------------------------------


FIXED_DT = datetime(2026, 5, 29, 12, 0, 0, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED_DT


# Orden canónico exigido por Requirement 4.1.
CANONICAL_ORDER: tuple[str, ...] = (
    "outbox-duplicates",
    "outbox-missing-prev-price",
    "outbox-bad-discounts",
    "outbox-stale",
)


# ---------------------------------------------------------------------------
# Stub task factory — produce subclases de ``BaseSaneamientoTask`` con
# comportamiento programable. Se devuelven CLASES (no instancias) porque
# el runner instancia desde el ``registry`` (``cls()``).
# ---------------------------------------------------------------------------


def _make_stub_task_class(
    *,
    name: str,
    candidates: list[CandidateRow] | None = None,
    apply_returns: int | None = None,
    raises_in_apply: BaseException | None = None,
    raises_in_select: BaseException | None = None,
    writes_last_attempt_at: bool = True,
) -> type[BaseSaneamientoTask]:
    """Crea una subclase de ``BaseSaneamientoTask`` con stubs deterministas.

    - ``candidates``: lista que devolverá ``select_candidates``.
    - ``apply_returns``: entero que devolverá ``apply``. Si es ``None``
      usa ``len(candidates)`` como default razonable.
    - ``raises_in_apply`` / ``raises_in_select``: si no son ``None``, la
      excepción correspondiente se levanta en lugar del retorno.
    - ``writes_last_attempt_at``: paridad con el flag homónimo de la
      task real (sólo relevante para distinguir
      ``outbox-missing-prev-price``; el stub no usa el ``apply``
      heredado, pero documentamos la intención).
    """

    _candidates = list(candidates or [])
    _apply_returns = (
        apply_returns if apply_returns is not None else len(_candidates)
    )
    _raises_in_apply = raises_in_apply
    _raises_in_select = raises_in_select
    _writes_last = writes_last_attempt_at
    _stub_name = name

    class _StubTask(BaseSaneamientoTask):
        name = _stub_name  # type: ignore[assignment]
        writes_last_attempt_at = _writes_last  # type: ignore[assignment]

        def select_candidates(
            self,
            conn: sqlite3.Connection,
            *,
            marketplace: str,
            clock: Callable[[], datetime],
        ) -> list[CandidateRow]:
            if _raises_in_select is not None:
                raise _raises_in_select
            # Devolver una copia para que el runner no pueda mutar la
            # plantilla compartida entre instancias.
            return list(_candidates)

        def apply(
            self,
            conn: sqlite3.Connection,
            candidates: list[CandidateRow],
            *,
            clock: Callable[[], datetime],
        ) -> int:
            if _raises_in_apply is not None:
                raise _raises_in_apply
            return _apply_returns

    _StubTask.__name__ = f"_StubTask_{name.replace('-', '_')}"
    return _StubTask


def _candidate(outbox_id: int, *, marketplace: str = "amazon") -> CandidateRow:
    return CandidateRow(
        outbox_id=outbox_id,
        marketplace=marketplace,
        title=f"Producto {outbox_id}",
    )


# ---------------------------------------------------------------------------
# Conexión sqlite mínima — sólo necesaria para que ``BEGIN IMMEDIATE``
# / ``COMMIT`` / ``ROLLBACK`` no fallen. Las stubs no leen ni escriben
# tablas reales: el runner les pasa la conn pero ellas devuelven datos
# canned. Por eso podemos prescindir del schema completo.
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """Conexión :memory: con ``isolation_level=None`` para permitir
    transacciones SQL explícitas (``BEGIN IMMEDIATE`` / ``COMMIT`` /
    ``ROLLBACK``) emitidas por el runner.
    """

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Wrapper de conn para grabar el SQL emitido por el runner.
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Wrapper de ``sqlite3.Connection`` que graba el SQL de cada
    ``execute(...)``. Reenvía cualquier otro atributo a la conn real
    vía ``__getattr__`` para que el runner pueda usarla como una
    conexión normal.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.executed_sql: list[str] = []

    def execute(
        self, sql: str, parameters: Any = (), /, *args: Any, **kwargs: Any
    ) -> sqlite3.Cursor:
        self.executed_sql.append(sql)
        if parameters == ():
            return self._conn.execute(sql, *args, **kwargs)
        return self._conn.execute(sql, parameters, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._conn, item)


# ---------------------------------------------------------------------------
# Spy para ``emit_event`` — graba cada llamada como un dict.
# ---------------------------------------------------------------------------


class _EmitSpy:
    """Reemplazo de ``emit_runtime_event`` que graba en memoria.

    La firma del callable real es
    ``emit_runtime_event(conn, *, kind, severity, payload) -> int``.
    El spy preserva esa firma y devuelve ids monotónicos partiendo de 1.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 1

    def __call__(
        self,
        conn: Any,
        *,
        kind: str,
        severity: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        self.calls.append(
            {
                "kind": kind,
                "severity": severity,
                "payload": dict(payload or {}),
            }
        )
        ev_id = self._next_id
        self._next_id += 1
        return ev_id

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]


# ---------------------------------------------------------------------------
# Helpers de construcción del registry.
# ---------------------------------------------------------------------------


def _empty_canonical_registry() -> dict[str, type[BaseSaneamientoTask]]:
    """Registry con las 4 tasks canónicas, todas devolviendo [] cands."""

    return {
        name: _make_stub_task_class(
            name=name,
            candidates=[],
            apply_returns=0,
            writes_last_attempt_at=(name != "outbox-missing-prev-price"),
        )
        for name in CANONICAL_ORDER
    }


def _build_args(
    *,
    task: str = "all",
    apply_mode: bool = False,
    marketplace: str = "all",
) -> SaneamientoRunArgs:
    return SaneamientoRunArgs(
        task=task, apply_mode=apply_mode, marketplace=marketplace
    )


# ===========================================================================
# 1) ``--task=all`` ejecuta las 4 tasks en orden canónico.
#    (Requirements 1.3, 4.1)
# ===========================================================================


class TestTaskAllRunsInCanonicalOrder:
    """Validates: Requirements 1.3, 4.1"""

    def test_task_all_runs_in_canonical_order(self) -> None:
        conn = _make_conn()
        registry = _empty_canonical_registry()
        emit = _EmitSpy()

        runner = SaneamientoRunner(
            conn,
            _build_args(task="all", apply_mode=False),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        report = runner.run()

        assert [t.task for t in report.tasks] == list(CANONICAL_ORDER)


# ===========================================================================
# 2) ``--task=outbox-stale`` ejecuta sólo esa task.
#    (Requirements 1.3)
# ===========================================================================


class TestTaskSpecificRunsOnlyOne:
    """Validates: Requirement 1.3"""

    def test_task_specific_runs_only_one(self) -> None:
        conn = _make_conn()
        registry = _empty_canonical_registry()
        emit = _EmitSpy()

        runner = SaneamientoRunner(
            conn,
            _build_args(task="outbox-stale", apply_mode=False),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        report = runner.run()

        assert len(report.tasks) == 1
        assert report.tasks[0].task == "outbox-stale"


# ===========================================================================
# 3) Apply_Mode usa transacción explícita por task con candidatos.
#    (Requirements 3.4, 3.5)
# ===========================================================================


class TestApplyModeUsesTransactionPerTask:
    """Validates: Requirements 3.4, 3.5"""

    def test_apply_mode_uses_transaction_per_task(self) -> None:
        # Cada task con 1 candidato → 4 transacciones distintas.
        registry: dict[str, type[BaseSaneamientoTask]] = {
            name: _make_stub_task_class(
                name=name,
                candidates=[_candidate(outbox_id=100 + i)],
                apply_returns=1,
                writes_last_attempt_at=(name != "outbox-missing-prev-price"),
            )
            for i, name in enumerate(CANONICAL_ORDER)
        }
        rec_conn = _RecordingConn(_make_conn())
        emit = _EmitSpy()

        runner = SaneamientoRunner(
            rec_conn,  # type: ignore[arg-type]
            _build_args(task="all", apply_mode=True),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        runner.run()

        # Filtrar el SQL transaccional (BEGIN IMMEDIATE / COMMIT / ROLLBACK).
        tx_sql = [
            s.strip().upper()
            for s in rec_conn.executed_sql
            if s.strip().upper()
            in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")
        ]

        # Debe haber al menos 4 BEGIN IMMEDIATE y 4 COMMIT — uno por task
        # con candidatos. Sin ROLLBACK porque ningún apply falló.
        assert tx_sql.count("BEGIN IMMEDIATE") == 4
        assert tx_sql.count("COMMIT") == 4
        assert tx_sql.count("ROLLBACK") == 0

        # Y el orden alterna BEGIN → COMMIT por task.
        assert tx_sql == ["BEGIN IMMEDIATE", "COMMIT"] * 4


# ===========================================================================
# 4) Excepción en ``apply`` → ROLLBACK + error en TaskResult, sigue.
#    (Requirements 3.5)
# ===========================================================================


class TestTaskApplyExceptionRollbackContinuesNext:
    """Validates: Requirement 3.5"""

    def test_apply_exception_rolls_back_and_continues(self) -> None:
        # 1ª task levanta RuntimeError("boom") en apply; las demás OK.
        registry: dict[str, type[BaseSaneamientoTask]] = {}
        for i, name in enumerate(CANONICAL_ORDER):
            if name == "outbox-duplicates":
                registry[name] = _make_stub_task_class(
                    name=name,
                    candidates=[_candidate(outbox_id=999)],
                    apply_returns=0,
                    raises_in_apply=RuntimeError("boom"),
                )
            else:
                registry[name] = _make_stub_task_class(
                    name=name,
                    candidates=[_candidate(outbox_id=100 + i)],
                    apply_returns=1,
                    writes_last_attempt_at=(
                        name != "outbox-missing-prev-price"
                    ),
                )

        rec_conn = _RecordingConn(_make_conn())
        emit = _EmitSpy()

        runner = SaneamientoRunner(
            rec_conn,  # type: ignore[arg-type]
            _build_args(task="all", apply_mode=True),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        report = runner.run()

        # 4 TaskResult, en orden canónico.
        assert [t.task for t in report.tasks] == list(CANONICAL_ORDER)

        # 1ª task: error capturado y affected=0.
        first = report.tasks[0]
        assert first.task == "outbox-duplicates"
        assert first.affected == 0
        assert first.error is not None
        assert first.error.startswith("RuntimeError:")
        assert "boom" in first.error

        # Resto de tasks: OK, sin error.
        for t in report.tasks[1:]:
            assert t.error is None
            assert t.affected == 1

        # SQL emitido: el BEGIN IMMEDIATE de la 1ª task se cierra con
        # ROLLBACK; las demás cierran con COMMIT.
        tx_sql = [
            s.strip().upper()
            for s in rec_conn.executed_sql
            if s.strip().upper()
            in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")
        ]
        assert tx_sql == [
            "BEGIN IMMEDIATE",
            "ROLLBACK",
            "BEGIN IMMEDIATE",
            "COMMIT",
            "BEGIN IMMEDIATE",
            "COMMIT",
            "BEGIN IMMEDIATE",
            "COMMIT",
        ]


# ===========================================================================
# 5) Apply_Mode con ``affected>0`` → emite ``saneamiento_task_applied``.
#    (Requirements 6.2, 6.5)
# ===========================================================================


class TestApplyModeEmitsTaskAppliedEventPerAffectedTask:
    """Validates: Requirements 6.2, 6.5"""

    def test_apply_mode_emits_task_applied_event_per_affected_task(
        self,
    ) -> None:
        # 2 tasks con affected>0, 2 tasks con candidates=[] (no emiten).
        registry: dict[str, type[BaseSaneamientoTask]] = {
            "outbox-duplicates": _make_stub_task_class(
                name="outbox-duplicates",
                candidates=[_candidate(outbox_id=10), _candidate(outbox_id=11)],
                apply_returns=2,
            ),
            "outbox-missing-prev-price": _make_stub_task_class(
                name="outbox-missing-prev-price",
                candidates=[],
                apply_returns=0,
                writes_last_attempt_at=False,
            ),
            "outbox-bad-discounts": _make_stub_task_class(
                name="outbox-bad-discounts",
                candidates=[_candidate(outbox_id=20)],
                apply_returns=1,
            ),
            "outbox-stale": _make_stub_task_class(
                name="outbox-stale",
                candidates=[],
                apply_returns=0,
            ),
        }
        emit = _EmitSpy()
        runner = SaneamientoRunner(
            _make_conn(),
            _build_args(task="all", apply_mode=True, marketplace="amazon"),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        runner.run()

        applied = emit.by_kind("saneamiento_task_applied")
        assert len(applied) == 2

        # Severities y payload contract.
        applied_by_task = {c["payload"]["task"]: c for c in applied}
        assert set(applied_by_task) == {
            "outbox-duplicates",
            "outbox-bad-discounts",
        }

        for call in applied:
            assert call["severity"] == "info"
            payload = call["payload"]
            assert "task" in payload
            assert "affected" in payload
            assert "marketplace_filter" in payload
            assert payload["marketplace_filter"] == "amazon"
            assert "ids_sample" in payload
            # ids_sample en el evento ≤ 20 (Requirement 6.5).
            assert len(payload["ids_sample"]) <= 20

        # Ningún evento dry_run / failed.
        assert emit.by_kind("saneamiento_run_dry_run") == []
        assert emit.by_kind("saneamiento_run_failed") == []


# ===========================================================================
# 6) ``outbox-missing-prev-price`` con affected>0 emite además el evento
#    legacy ``outbox_cleanup_missing_prev_price``.
#    (Requirements 6.4)
# ===========================================================================


class TestOutboxMissingPrevPriceWithAffectedEmitsLegacyEvent:
    """Validates: Requirement 6.4"""

    def test_legacy_event_is_emitted_when_missing_prev_price_affects_rows(
        self,
    ) -> None:
        candidates = [
            _candidate(outbox_id=201),
            _candidate(outbox_id=205),
            _candidate(outbox_id=210),
            _candidate(outbox_id=217),
        ]
        registry: dict[str, type[BaseSaneamientoTask]] = {
            "outbox-duplicates": _make_stub_task_class(
                name="outbox-duplicates", candidates=[], apply_returns=0
            ),
            "outbox-missing-prev-price": _make_stub_task_class(
                name="outbox-missing-prev-price",
                candidates=candidates,
                apply_returns=4,
                writes_last_attempt_at=False,
            ),
            "outbox-bad-discounts": _make_stub_task_class(
                name="outbox-bad-discounts", candidates=[], apply_returns=0
            ),
            "outbox-stale": _make_stub_task_class(
                name="outbox-stale", candidates=[], apply_returns=0
            ),
        }
        emit = _EmitSpy()
        runner = SaneamientoRunner(
            _make_conn(),
            _build_args(task="all", apply_mode=True, marketplace="all"),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        runner.run()

        # Hay exactamente UN ``saneamiento_task_applied`` (la única task
        # con affected>0 es ``outbox-missing-prev-price``).
        applied = emit.by_kind("saneamiento_task_applied")
        assert len(applied) == 1
        assert applied[0]["payload"]["task"] == "outbox-missing-prev-price"
        assert applied[0]["payload"]["affected"] == 4

        # Y exactamente UN evento legacy.
        legacy = emit.by_kind("outbox_cleanup_missing_prev_price")
        assert len(legacy) == 1
        legacy_call = legacy[0]
        assert legacy_call["severity"] == "info"
        legacy_payload = legacy_call["payload"]
        assert legacy_payload["discarded_count"] == 4
        assert legacy_payload["marketplace_filter"] == "all"
        assert "ids_sample" in legacy_payload
        # El ids_sample del legacy ⊆ TaskResult.ids_sample.
        legacy_ids = set(legacy_payload["ids_sample"])
        applied_ids = set(applied[0]["payload"]["ids_sample"])
        assert legacy_ids <= applied_ids


# ===========================================================================
# 7) Dry_Run emite exactamente UN ``saneamiento_run_dry_run`` y CERO
#    ``saneamiento_task_applied``.
#    (Requirements 6.3, 6.5)
# ===========================================================================


class TestDryRunEmitsSingleRunDryRunEvent:
    """Validates: Requirements 6.3, 6.5"""

    def test_dry_run_emits_single_run_dry_run_event(self) -> None:
        # Stubs con candidatos: en Dry_Run NO debe llamarse a apply, así
        # que ``apply_returns`` no afecta. inspected != 0 para validar
        # que el evento dry-run reporta los conteos.
        registry: dict[str, type[BaseSaneamientoTask]] = {
            "outbox-duplicates": _make_stub_task_class(
                name="outbox-duplicates",
                candidates=[_candidate(1), _candidate(2)],
                apply_returns=999,  # nunca se llama en Dry_Run
            ),
            "outbox-missing-prev-price": _make_stub_task_class(
                name="outbox-missing-prev-price",
                candidates=[],
                apply_returns=0,
                writes_last_attempt_at=False,
            ),
            "outbox-bad-discounts": _make_stub_task_class(
                name="outbox-bad-discounts",
                candidates=[_candidate(3)],
                apply_returns=999,
            ),
            "outbox-stale": _make_stub_task_class(
                name="outbox-stale",
                candidates=[],
                apply_returns=0,
            ),
        }
        emit = _EmitSpy()
        runner = SaneamientoRunner(
            _make_conn(),
            _build_args(task="all", apply_mode=False, marketplace="all"),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        report = runner.run()

        # Exactamente 1 evento dry-run, 0 task-applied.
        dry = emit.by_kind("saneamiento_run_dry_run")
        applied = emit.by_kind("saneamiento_task_applied")
        assert len(dry) == 1
        assert applied == []

        # Severity y payload shape.
        assert dry[0]["severity"] == "info"
        dry_payload = dry[0]["payload"]
        assert "tasks" in dry_payload
        # Una entrada por task ejecutada con su ``inspected``.
        names_in_payload = {t["task"] for t in dry_payload["tasks"]}
        assert names_in_payload == set(CANONICAL_ORDER)

        # Y los inspected casan con el TaskResult correspondiente.
        report_inspected = {t.task: t.inspected for t in report.tasks}
        for entry in dry_payload["tasks"]:
            assert entry["inspected"] == report_inspected[entry["task"]]

        # Todos los TaskResult con affected=0 (Dry_Run no muta).
        assert all(t.affected == 0 for t in report.tasks)


# ===========================================================================
# 8) Tasks con ``affected = 0`` NO emiten ``saneamiento_task_applied``.
#    (Requirements 6.2)
# ===========================================================================


class TestZeroAffectedDoesNotEmitTaskApplied:
    """Validates: Requirement 6.2"""

    def test_zero_affected_does_not_emit_task_applied_event(self) -> None:
        # La task selecciona 5 candidatos pero ``apply`` devuelve 0
        # (p. ej. todos quedaron fuera por la guard
        # ``WHERE state='pending'``). El runner NO debe emitir el evento.
        registry: dict[str, type[BaseSaneamientoTask]] = {
            "outbox-duplicates": _make_stub_task_class(
                name="outbox-duplicates",
                candidates=[
                    _candidate(1),
                    _candidate(2),
                    _candidate(3),
                    _candidate(4),
                    _candidate(5),
                ],
                apply_returns=0,  # affected=0 a pesar de inspected=5
            ),
            "outbox-missing-prev-price": _make_stub_task_class(
                name="outbox-missing-prev-price",
                candidates=[],
                apply_returns=0,
                writes_last_attempt_at=False,
            ),
            "outbox-bad-discounts": _make_stub_task_class(
                name="outbox-bad-discounts",
                candidates=[],
                apply_returns=0,
            ),
            "outbox-stale": _make_stub_task_class(
                name="outbox-stale",
                candidates=[],
                apply_returns=0,
            ),
        }
        emit = _EmitSpy()
        runner = SaneamientoRunner(
            _make_conn(),
            _build_args(task="all", apply_mode=True, marketplace="all"),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )
        report = runner.run()

        # Inspected captura los 5 candidatos seleccionados, pero
        # affected=0 → ningún evento.
        first = report.tasks[0]
        assert first.task == "outbox-duplicates"
        assert first.inspected == 5
        assert first.affected == 0

        assert emit.by_kind("saneamiento_task_applied") == []


# ===========================================================================
# 9) Excepción no controlada del orquestador → ``saneamiento_run_failed``.
#    (Requirements 1.9)
# ===========================================================================


class TestUnhandledRunnerExceptionEmitsRunFailed:
    """Validates: Requirement 1.9"""

    def test_unhandled_exception_in_select_emits_run_failed_and_reraises(
        self,
    ) -> None:
        # La 2ª task (``outbox-missing-prev-price``) levanta una
        # excepción en ``select_candidates`` (fuera del try/except del
        # apply). El runner debe emitir ``saneamiento_run_failed`` y
        # re-raise.
        boom = ValueError("orchestrator-blew-up")
        registry: dict[str, type[BaseSaneamientoTask]] = {
            "outbox-duplicates": _make_stub_task_class(
                name="outbox-duplicates",
                candidates=[],
                apply_returns=0,
            ),
            "outbox-missing-prev-price": _make_stub_task_class(
                name="outbox-missing-prev-price",
                candidates=[],
                apply_returns=0,
                raises_in_select=boom,
                writes_last_attempt_at=False,
            ),
            "outbox-bad-discounts": _make_stub_task_class(
                name="outbox-bad-discounts",
                candidates=[],
                apply_returns=0,
            ),
            "outbox-stale": _make_stub_task_class(
                name="outbox-stale",
                candidates=[],
                apply_returns=0,
            ),
        }
        emit = _EmitSpy()
        runner = SaneamientoRunner(
            _make_conn(),
            _build_args(task="all", apply_mode=True, marketplace="all"),
            clock=fixed_clock,
            registry=registry,
            emit_event=emit,
        )

        with pytest.raises(ValueError, match="orchestrator-blew-up"):
            runner.run()

        # Se emitió exactamente UN saneamiento_run_failed con severity error.
        failed = emit.by_kind("saneamiento_run_failed")
        assert len(failed) == 1
        ev = failed[0]
        assert ev["severity"] == "error"

        payload = ev["payload"]
        # Campos del payload (matriz design.md → "saneamiento_run_failed").
        assert payload["error_type"] == "ValueError"
        assert payload["error_message"] == "orchestrator-blew-up"
        # ``outbox-duplicates`` completó antes del fallo.
        assert payload["tasks_completed"] == ["outbox-duplicates"]
        # Las 3 tasks restantes (incluida la que falló) quedan pendientes.
        assert payload["tasks_pending"] == [
            "outbox-missing-prev-price",
            "outbox-bad-discounts",
            "outbox-stale",
        ]
        assert payload["mode"] == "apply"
        assert payload["marketplace_filter"] == "all"
