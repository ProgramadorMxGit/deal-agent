"""Lockfile dedicado del Bot_Saneamiento.

Este módulo implementa :class:`SaneamientoLock`, un wrapper cooperativo en
disco que protege la ejecución del Bot_Saneamiento contra invocaciones
concurrentes (Requirement 2.6, 2.7). A diferencia de
:class:`ofertas_hunter.mcp.lockfile.FileLock`, este lock:

- Convive tolerantemente con ``data/mcp_serve.lock``: si existe y está vivo,
  registra la concurrencia en ``LockAcquisition.mcp_serve_active`` pero
  **nunca** modifica ese archivo (Requirement 2.5).
- Detecta locks huérfanos (pid muerto) y los sobrescribe sin error.
- Persiste el holder como JSON UTF-8 con la forma::

      {"pid": <int>, "scope": "saneamiento", "started_at": "<ISO-Z>"}

La detección cross-platform de pid vivo se reutiliza desde
``ofertas_hunter.mcp.lockfile._pid_is_alive`` para garantizar paridad con
el lock del MCP server.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# paridad con mcp.lockfile
from ofertas_hunter.mcp.lockfile import _pid_is_alive

from .time_source import now_utc_iso

__all__ = [
    "LockAcquisition",
    "SaneamientoLock",
    "SaneamientoLockBusy",
]


class SaneamientoLockBusy(RuntimeError):
    """Levantada cuando otro Bot_Saneamiento vivo ya tiene el lock.

    Atributo ``holder``: ``dict`` con al menos ``pid`` y ``started_at`` del
    proceso dueño del lock activo. El CLI usa estos campos para imprimir el
    mensaje exigido por el Requirement 2.7 antes de salir con código ``2``.
    """

    holder: dict[str, Any]

    def __init__(self, *, holder: dict[str, Any]) -> None:
        self.holder = holder
        pid = holder.get("pid")
        started_at = holder.get("started_at")
        super().__init__(
            f"data/saneamiento.lock activo: pid={pid} started_at={started_at}"
        )


@dataclass(frozen=True)
class LockAcquisition:
    """Resultado de :meth:`SaneamientoLock.acquire`.

    - ``mcp_serve_active``: ``True`` si ``data/mcp_serve.lock`` existía y
      su pid estaba vivo en el momento del ``acquire()``. Solo informativo;
      el Bot_Saneamiento nunca aborta por este motivo (Requirement 2.5).
    - ``holder``: dict con ``pid`` y ``started_at`` recién escritos en
      ``data/saneamiento.lock`` por esta adquisición.
    """

    mcp_serve_active: bool
    holder: dict[str, Any]


@dataclass
class SaneamientoLock:
    """Lock cooperativo del Bot_Saneamiento.

    Parámetros:

    - ``path``: ruta al archivo de lock propio (típicamente
      ``data/saneamiento.lock``).
    - ``mcp_lock_path``: ruta opcional al lock del MCP server. Solo se lee
      para detectar concurrencia; nunca se modifica ni borra.
    - ``pid_alive``: callable inyectable usado para chequear si un pid sigue
      vivo. Por defecto delega en
      :func:`ofertas_hunter.mcp.lockfile._pid_is_alive`. Se expone como
      keyword-only para permitir tests deterministas sin depender del SO.
    """

    path: Path
    mcp_lock_path: Optional[Path] = None
    pid_alive: Callable[[int], bool] = field(
        default=_pid_is_alive, kw_only=True
    )

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def acquire(self) -> LockAcquisition:
        """Adquiere el lock o levanta :class:`SaneamientoLockBusy`.

        Pasos:

        1. Lee ``self.path``. Si existe, está parseable y su ``pid`` es
           distinto del actual y sigue vivo según ``pid_alive``, levanta
           ``SaneamientoLockBusy`` sin modificar el archivo.
        2. En caso contrario (no existe, ilegible o lock huérfano)
           sobrescribe el archivo con el holder propio.
        3. Lee ``mcp_lock_path`` (si se proporcionó) en modo solo lectura
           para popular ``LockAcquisition.mcp_serve_active``.
        """

        existing = self._read(self.path)
        if existing is not None:
            holder_pid = existing.get("pid")
            if (
                isinstance(holder_pid, int)
                and holder_pid != os.getpid()
                and self.pid_alive(holder_pid)
            ):
                raise SaneamientoLockBusy(holder=existing)
            # Lock huérfano (pid muerto) o de nuestro propio pid: lo
            # sobreescribimos transparentemente más abajo.

        started_at = now_utc_iso()
        own_pid = os.getpid()
        payload: dict[str, Any] = {
            "pid": own_pid,
            "scope": "saneamiento",
            "started_at": started_at,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        mcp_serve_active = False
        if self.mcp_lock_path is not None:
            mcp_payload = self._read(self.mcp_lock_path)
            if mcp_payload is not None:
                mcp_pid = mcp_payload.get("pid")
                if isinstance(mcp_pid, int) and self.pid_alive(mcp_pid):
                    mcp_serve_active = True

        return LockAcquisition(
            mcp_serve_active=mcp_serve_active,
            holder={"pid": own_pid, "started_at": started_at},
        )

    def release(self) -> None:
        """Libera el lock si lo poseemos. Idempotente y race-safe.

        - Si el archivo ya no existe, retorna silenciosamente.
        - Si el archivo existe pero pertenece a otro pid (caso anómalo),
          NO lo borra para no liberar locks ajenos por error.
        - Tolera carreras de filesystem (``FileNotFoundError`` durante
          ``unlink`` se ignora).
        """

        payload = self._read(self.path)
        if payload is None:
            # Ya no existe, o ilegible: nada que liberar.
            return
        if payload.get("pid") != os.getpid():
            # No es nuestro lock; respetar al holder real.
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            # Carrera: alguien ya lo borró entre el _read y el unlink.
            return

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> Optional[dict[str, Any]]:
        """Lee un archivo de lock; devuelve None si no existe o no parsea."""

        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
