"""Entrypoint del subcomando ``saneamiento``.

Esta función es invocada desde ``ofertas_hunter.__main__`` cuando el
operador ejecuta::

    python -m ofertas_hunter saneamiento [--task ...] [--apply] [--marketplace ...]

Contrato (ver ``.kiro/specs/bot-saneamiento-vps/design.md`` →
Components and Interfaces → ``cli.cmd_saneamiento`` y la matriz de
exit codes en Error Handling):

1. Parsear ``args`` (``argparse.Namespace`` con atributos ``task``,
   ``apply`` y ``marketplace``) a :class:`SaneamientoRunArgs`.
2. Abrir la DB con :func:`ofertas_hunter.db.connect`. Si ``connect()``
   levanta excepción → exit ``1`` SIN haber tocado el lockfile
   (Requirement 1.9).
3. Adquirir :class:`SaneamientoLock` apuntando a
   ``data/saneamiento.lock`` con ``mcp_lock_path=data/mcp_serve.lock``.
   Si ``acquire()`` levanta :class:`SaneamientoLockBusy` → imprime el
   ``pid`` y ``started_at`` del holder en stdout, cierra la conn y
   sale con código ``2`` sin haber tocado la DB (Requirement 2.7).
4. Construir :class:`SaneamientoRunner` y llamar ``.run()``. Si la
   run levanta excepción no controlada → libera lock, cierra conn y
   sale con código ``1``. El runner ya emite
   ``saneamiento_run_failed`` antes de re-lanzar.
5. Reescribir ``mcp_serve_active`` en el report con el valor real
   reportado por :class:`LockAcquisition` (el runner por sí solo no
   conoce ese flag y devuelve ``False``) — Requirement 2.5.
6. Imprimir el ``SaneamientoReport`` por stdout vía
   :func:`print_to_stdout` (Requirement 1.8 / 3.3 / 6.1).
7. Liberar el lock, cerrar la conn y devolver ``0``.

``SaneamientoLock``, ``SaneamientoRunner`` y ``connect`` se
re-exportan a nivel de módulo para que los tests puedan
``monkeypatch.setattr(cli, "...", fake)`` y simular los distintos
escenarios sin tocar disco/DB reales.
"""

from __future__ import annotations

import argparse
import dataclasses
import sqlite3
from pathlib import Path
from typing import Optional

from ofertas_hunter.config import PROJECT_ROOT
from ofertas_hunter.db import connect

from .lockfile import (
    LockAcquisition,
    SaneamientoLock,
    SaneamientoLockBusy,
)
from .report import SaneamientoReport, print_to_stdout
from .runner import SaneamientoRunArgs, SaneamientoRunner

__all__ = ["cmd_saneamiento"]


_DATA_DIR = PROJECT_ROOT / "data"
_SANEAMIENTO_LOCK_PATH = _DATA_DIR / "saneamiento.lock"
_MCP_SERVE_LOCK_PATH = _DATA_DIR / "mcp_serve.lock"


def _build_run_args(args: argparse.Namespace) -> SaneamientoRunArgs:
    """Traduce el ``argparse.Namespace`` recibido del subcomando a
    :class:`SaneamientoRunArgs`.

    Los nombres canónicos de los flags son ``task``, ``apply`` y
    ``marketplace`` (ver wiring en ``__main__.py``). El bool
    ``apply`` se renombra a ``apply_mode`` en el dataclass del
    runner.
    """

    return SaneamientoRunArgs(
        task=args.task,
        apply_mode=bool(args.apply),
        marketplace=args.marketplace,
    )


def _close_quietly(conn: sqlite3.Connection) -> None:
    """Cierra ``conn`` ignorando cualquier excepción de cierre.

    El estado de la run ya está reportado a stdout/runtime_events
    antes de llegar aquí; un fallo en el cierre no debe enmascarar
    el exit code calculado.
    """

    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _release_quietly(lock: SaneamientoLock) -> None:
    """Libera ``lock`` ignorando errores. ``release()`` ya es
    idempotente y race-safe, pero envolvemos por defensa para que
    el cleanup nunca propague excepciones que cambien el exit code.
    """

    try:
        lock.release()
    except Exception:  # noqa: BLE001
        pass


def _print_lock_busy_holder(holder: dict) -> None:
    """Imprime el mensaje exigido por el Requirement 2.7.

    Formato fijo (paridad con el ``SaneamientoLockBusy.__init__``)::

        ERROR: data/saneamiento.lock activo: pid=<pid> started_at=<iso>
    """

    pid = holder.get("pid")
    started_at = holder.get("started_at")
    print(
        f"ERROR: data/saneamiento.lock activo: "
        f"pid={pid} started_at={started_at}"
    )


def cmd_saneamiento(args: argparse.Namespace) -> int:
    """Entrypoint del subcomando ``python -m ofertas_hunter saneamiento``.

    Devuelve el exit code según la matriz del design:

    - ``0`` — Saneamiento_Run completa sin excepción no controlada.
    - ``1`` — ``connect()`` falla, o el runner levanta una excepción
      no manejada (el runner ya emitió ``saneamiento_run_failed``).
    - ``2`` — ``data/saneamiento.lock`` activo de otro proceso vivo.
    """

    run_args = _build_run_args(args)

    # ------------------------------------------------------------------
    # 1) Abrir DB ANTES de tocar el lock (Requirement 1.9).
    # ------------------------------------------------------------------
    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: connect() failed: {type(exc).__name__}: {exc}"
        )
        return 1

    # ------------------------------------------------------------------
    # 2) Adquirir el lock. Si ya hay otro saneamiento vivo → exit 2.
    # ------------------------------------------------------------------
    lock = SaneamientoLock(
        path=_SANEAMIENTO_LOCK_PATH,
        mcp_lock_path=_MCP_SERVE_LOCK_PATH,
    )
    acquisition: Optional[LockAcquisition]
    try:
        acquisition = lock.acquire()
    except SaneamientoLockBusy as busy:
        # ``sin tocar DB``: dejamos la conn abierta para que el caller
        # (y los tests) puedan inspeccionar el estado intacto. El
        # proceso es oneshot y termina inmediatamente, así que la
        # conn la libera el OS al salir.
        _print_lock_busy_holder(busy.holder)
        return 2
    except Exception as exc:  # noqa: BLE001
        # Cualquier otro fallo del lock (I/O del filesystem, etc.) se
        # trata como error genérico de la run — exit 1.
        print(
            f"ERROR: lock acquire failed: {type(exc).__name__}: {exc}"
        )
        return 1

    # ------------------------------------------------------------------
    # 3) Ejecutar la run. El runner gestiona transacciones por task y
    #    emite eventos. Cualquier excepción que escape ya viene con
    #    ``saneamiento_run_failed`` emitido (test_runner cubre eso).
    # ------------------------------------------------------------------
    try:
        runner = SaneamientoRunner(conn, run_args)
        report = runner.run()
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: saneamiento_run_failed: "
            f"{type(exc).__name__}: {exc}"
        )
        _release_quietly(lock)
        _close_quietly(conn)
        return 1


    # ------------------------------------------------------------------
    # 4) Reescribir mcp_serve_active con el valor real del lock layer.
    #    El runner devuelve ``False`` por defecto porque no conoce el
    #    estado de ``data/mcp_serve.lock`` (Requirement 2.5).
    # ------------------------------------------------------------------
    final_report: SaneamientoReport = dataclasses.replace(
        report,
        mcp_serve_active=acquisition.mcp_serve_active,
    )

    # ------------------------------------------------------------------
    # 5) Render legible + JSON + literal Dry_Run cuando aplica.
    # ------------------------------------------------------------------
    print_to_stdout(final_report)

    # ------------------------------------------------------------------
    # 6) Cleanup determinista: lock primero, luego conn. Ambos son
    #    no-throwing aquí.
    # ------------------------------------------------------------------
    _release_quietly(lock)
    _close_quietly(conn)
    return 0
