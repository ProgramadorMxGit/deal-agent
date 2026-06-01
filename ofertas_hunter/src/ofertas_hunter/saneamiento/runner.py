"""Orquestador del ``Saneamiento_Run``.

Provee:

- :class:`SaneamientoRunArgs` — frozen dataclass con ``task``,
  ``apply_mode`` y ``marketplace``.
- :class:`SaneamientoRunner` — orquesta las ``Saneamiento_Task`` del
  ``TASK_REGISTRY`` (o un subset según ``args.task``) en orden
  canónico, gestiona transacciones por task en Apply_Mode y emite los
  eventos de auditoría según la matriz definida en
  ``.kiro/specs/bot-saneamiento-vps/design.md``.

Contrato (ver design.md → Components and Interfaces → ``SaneamientoRunner``):

1. Resolver el subset de tasks: ``"all"`` ⇒ todas en orden canónico
   (orden de iteración del ``TASK_REGISTRY``), de lo contrario una
   sola task identificada por su ``name``.
2. Por cada task, llamar siempre ``task.select_candidates(...)``.
3. En Apply_Mode con ``len(candidates) > 0``:
   ``self._conn.execute("BEGIN IMMEDIATE")`` →
   ``task.apply(...)`` →
   ``self._conn.execute("COMMIT")``.
   Si ``apply`` lanza cualquier excepción → ``ROLLBACK`` y la task
   continúa con ``affected=0`` y ``error="<ExcType>: <msg>"``.
4. Tras procesar todas las tasks, emitir los ``runtime_events``
   correspondientes (``saneamiento_task_applied`` por task con
   ``affected > 0`` en Apply_Mode, evento legacy
   ``outbox_cleanup_missing_prev_price`` cuando aplica, o un único
   ``saneamiento_run_dry_run`` agregado en Dry_Run).
5. Cualquier excepción no controlada que escape al orquestador (p.ej.
   en ``select_candidates``, fuera del try/except del apply) emite
   ``saneamiento_run_failed`` con ``tasks_completed`` /
   ``tasks_pending`` y se re-lanza para que ``cli`` la traduzca a
   exit code 1.

Cubre Requirements 1.3, 3.4, 3.5, 4.1, 4.8, 6.2, 6.3, 6.4, 6.5.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from ofertas_hunter.runtime.events import emit_runtime_event

from .report import SaneamientoReport, TaskResult
from .tasks import TASK_REGISTRY
from .tasks.base import BaseSaneamientoTask
from .time_source import now_utc, now_utc_iso

__all__ = ["SaneamientoRunArgs", "SaneamientoRunner"]


# Cap del campo ``ids_sample`` en los payloads de ``runtime_events``
# (Requirement 6.5). El cap del ``TaskResult.ids_sample`` (≤ 30,
# Requirement 6.1) ya lo enforce el propio ``TaskResult``.
_EVENT_IDS_SAMPLE_CAP = 20

# Cap del ``ids_sample`` que el runner construye y guarda en cada
# ``TaskResult``. Coincide con el invariante de ``TaskResult`` para
# evitar levantar ``ValueError`` desde dentro del runner.
_RESULT_IDS_SAMPLE_CAP = 30


@dataclass(frozen=True)
class SaneamientoRunArgs:
    """Argumentos inmutables del ``Saneamiento_Run``.

    - ``task``: ``"all"`` o el ``name`` canónico de una task del registry.
    - ``apply_mode``: ``False`` ⇒ Dry_Run; ``True`` ⇒ Apply_Mode.
    - ``marketplace``: ``"all"``, ``"amazon"`` o ``"mercadolibre"`` —
      forwardeado a cada ``select_candidates`` (Requirement 4.7).
    """

    task: str
    apply_mode: bool
    marketplace: str


class SaneamientoRunner:
    """Orquesta una ejecución del Bot_Saneamiento.

    No abre ni cierra la conexión: el caller (``cli.cmd_saneamiento``)
    es responsable del lifecycle de ``conn``. El runner tampoco maneja
    el lockfile — eso vive en ``saneamiento.lockfile`` y se enlaza
    desde la CLI.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        args: SaneamientoRunArgs,
        *,
        clock: Callable[[], datetime] = now_utc,
        registry: Mapping[str, type[BaseSaneamientoTask]] | None = None,
        emit_event: Callable[..., int] = emit_runtime_event,
    ) -> None:
        self._conn = conn
        self._args = args
        self._clock = clock
        self._registry: Mapping[str, type[BaseSaneamientoTask]] = (
            registry if registry is not None else TASK_REGISTRY
        )
        self._emit_event = emit_event

    # ------------------------------------------------------------------
    # API pública.
    # ------------------------------------------------------------------

    def run(self) -> SaneamientoReport:
        """Ejecuta el run y devuelve el ``SaneamientoReport`` agregado.

        Re-lanza cualquier excepción que no haya sido absorbida por el
        try/except del ``apply`` (p.ej. fallos en
        ``select_candidates``), tras emitir
        ``saneamiento_run_failed``.
        """

        started_at = now_utc_iso(self._clock)
        mode: str = "apply" if self._args.apply_mode else "dry-run"
        marketplace = self._args.marketplace
        names = self._selected_task_names()

        results: list[TaskResult] = []
        completed_names: list[str] = []

        try:
            for name in names:
                result = self._run_one_task(name, mode=mode, marketplace=marketplace)
                results.append(result)
                completed_names.append(name)

            # Sólo emitimos los eventos post-run si no hubo excepción
            # propagada al orquestador. Errores capturados por el
            # try/except del apply NO bloquean estos eventos: las
            # tasks restantes (con affected>0) siguen emitiendo sus
            # propios ``saneamiento_task_applied``.
            self._emit_post_run_events(results, marketplace=marketplace)

        except BaseException as exc:  # noqa: BLE001 — re-raise tras emitir.
            self._emit_run_failed(
                exc,
                mode=mode,
                marketplace=marketplace,
                completed_names=completed_names,
                all_names=names,
            )
            raise

        finished_at = now_utc_iso(self._clock)

        return SaneamientoReport(
            started_at=started_at,
            finished_at=finished_at,
            mode=mode,  # type: ignore[arg-type]
            marketplace_filter=marketplace,
            # ``mcp_serve_active`` lo determina el lock layer; el
            # runner por sí solo no lo conoce. La ``cli`` puede
            # construir el report final con el valor real del
            # ``LockAcquisition`` si lo necesita.
            mcp_serve_active=False,
            tasks=results,
        )

    # ------------------------------------------------------------------
    # Helpers internos.
    # ------------------------------------------------------------------

    def _selected_task_names(self) -> list[str]:
        """Devuelve la lista de nombres de tasks a ejecutar.

        ``"all"`` ⇒ todos los nombres del registry en orden de
        iteración (orden canónico, Requirement 4.1). En caso contrario,
        una lista con la única task seleccionada.

        Levanta ``ValueError`` si ``task`` no es ``"all"`` y no existe
        en el registry — defensivo, ya que la CLI valida el input.
        """

        if self._args.task == "all":
            return list(self._registry.keys())
        if self._args.task not in self._registry:
            raise ValueError(
                f"task desconocida: {self._args.task!r} "
                f"(esperada 'all' o una de {list(self._registry.keys())})"
            )
        return [self._args.task]

    def _run_one_task(
        self,
        name: str,
        *,
        mode: str,
        marketplace: str,
    ) -> TaskResult:
        """Ejecuta ``select_candidates`` y, si aplica, la transacción.

        Devuelve un ``TaskResult`` consistente con sus invariantes
        (``affected ≤ inspected``; ``affected = 0`` en Dry_Run;
        ``len(ids_sample) ≤ 30``). Cualquier excepción durante
        ``select_candidates`` se propaga al ``run()`` para que la
        capture el guard global (``saneamiento_run_failed`` +
        re-raise). Las excepciones durante ``apply`` se absorben aquí
        con ``ROLLBACK`` y se reflejan en ``TaskResult.error``
        (Requirement 3.5).
        """

        task_cls = self._registry[name]
        task = task_cls()

        # ``select_candidates`` se ejecuta SIEMPRE — incluso en Dry_Run
        # — para poder reportar ``inspected`` (Requirement 3.2).
        candidates = task.select_candidates(
            self._conn,
            marketplace=marketplace,
            clock=self._clock,
        )

        inspected = len(candidates)

        # ``ids_sample`` ascendente y acotado al cap del TaskResult.
        ids_sample: list[int] = sorted(c.outbox_id for c in candidates)[
            :_RESULT_IDS_SAMPLE_CAP
        ]

        affected = 0
        error: str | None = None

        if self._args.apply_mode and candidates:
            try:
                # Texto exacto del SQL: los tests filtran por igualdad
                # exacta y el design especifica ``BEGIN IMMEDIATE``
                # (no ``BEGIN``) para arrancar una transacción
                # exclusiva (Requirement 3.4).
                self._conn.execute("BEGIN IMMEDIATE")
                affected = task.apply(
                    self._conn,
                    candidates,
                    clock=self._clock,
                )
                self._conn.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                # Rollback best-effort; si el ROLLBACK falla
                # (p.ej. la conexión ya está rota), igual seguimos
                # con la siguiente task — Requirement 3.5.
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                affected = 0
                error = f"{type(exc).__name__}: {exc}"

        # Salvaguarda defensiva: respetar el invariante
        # ``affected <= inspected``. En la práctica el ``apply``
        # devuelve ``rowcount`` sobre un subconjunto de los candidatos
        # (gracias al guard ``state='pending'`` del UPDATE), por lo
        # que esta cota se cumple siempre. La aplicamos por si una
        # task custom devuelve un valor mayor.
        if affected > inspected:
            affected = inspected

        return TaskResult(
            task=name,
            mode=mode,  # type: ignore[arg-type]
            marketplace_filter=marketplace,
            inspected=inspected,
            affected=affected,
            ids_sample=ids_sample,
            error=error,
        )

    # ------------------------------------------------------------------
    # Emisión de eventos.
    # ------------------------------------------------------------------

    def _emit_post_run_events(
        self,
        results: list[TaskResult],
        *,
        marketplace: str,
    ) -> None:
        """Emite los eventos finales según el modo del run.

        Apply_Mode:
        - ``saneamiento_task_applied`` por cada task con
          ``affected > 0`` (Requirement 6.2 / 6.5).
        - ``outbox_cleanup_missing_prev_price`` (legacy, Requirement
          6.4) cuando la task ``outbox-missing-prev-price`` afectó
          filas.

        Dry_Run:
        - Un único ``saneamiento_run_dry_run`` agregado con la
          lista ``[{task, inspected}, ...]`` (Requirement 6.3).
        """

        if self._args.apply_mode:
            for r in results:
                if r.affected <= 0 or r.error is not None:
                    continue

                applied_payload: dict[str, Any] = {
                    "task": r.task,
                    "affected": r.affected,
                    "marketplace_filter": r.marketplace_filter,
                    "ids_sample": list(
                        r.ids_sample[:_EVENT_IDS_SAMPLE_CAP]
                    ),
                }
                self._emit_event(
                    self._conn,
                    kind="saneamiento_task_applied",
                    severity="info",
                    payload=applied_payload,
                )

                # Evento legacy (Requirement 6.4): se emite además
                # para preservar las alarmas existentes que escuchaban
                # ``outbox_cleanup_missing_prev_price``.
                if r.task == "outbox-missing-prev-price":
                    legacy_payload: dict[str, Any] = {
                        "discarded_count": r.affected,
                        "marketplace_filter": r.marketplace_filter,
                        "ids_sample": list(
                            r.ids_sample[:_EVENT_IDS_SAMPLE_CAP]
                        ),
                    }
                    self._emit_event(
                        self._conn,
                        kind="outbox_cleanup_missing_prev_price",
                        severity="info",
                        payload=legacy_payload,
                    )
            return

        # Dry_Run — un único evento agregado por run.
        dry_payload: dict[str, Any] = {
            "marketplace_filter": marketplace,
            "tasks": [
                {"task": r.task, "inspected": r.inspected}
                for r in results
            ],
        }
        self._emit_event(
            self._conn,
            kind="saneamiento_run_dry_run",
            severity="info",
            payload=dry_payload,
        )

    def _emit_run_failed(
        self,
        exc: BaseException,
        *,
        mode: str,
        marketplace: str,
        completed_names: list[str],
        all_names: list[str],
    ) -> None:
        """Emite ``saneamiento_run_failed`` antes de re-lanzar.

        ``tasks_pending`` incluye la task que falló y las que aún no
        habían ejecutado, en el orden canónico de ``all_names``.
        Si el propio ``emit_event`` lanza, lo silenciamos para no
        reemplazar la excepción original; el caller verá la traza
        original tras el ``raise`` del ``run()``.
        """

        completed_set = set(completed_names)
        pending = [n for n in all_names if n not in completed_set]

        payload: dict[str, Any] = {
            "marketplace_filter": marketplace,
            "mode": mode,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "tasks_completed": list(completed_names),
            "tasks_pending": pending,
        }
        try:
            self._emit_event(
                self._conn,
                kind="saneamiento_run_failed",
                severity="error",
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            # Mejor preservar la excepción original que enmascararla
            # con un fallo del logger.
            pass
