"""Lockfile cooperativo en disco para detectar concurrencia entre `run` y `mcp-serve`.

Escribe un JSON con `pid`, `scope` y `created_at` en la ruta indicada. Si el
archivo existe y el proceso dueño sigue vivo, levanta `LockConflict`. Si el
proceso ya no existe (lock huérfano), sobrescribe.

Multiplataforma:

- En Windows usa `OpenProcess` + `GetExitCodeProcess` para detectar pid vivos
  sin requerir privilegios elevados.
- En POSIX usa `os.kill(pid, 0)` (no envía señal, sólo verifica existencia).

No requiere bloqueo a nivel de filesystem. Es cooperativo: cualquier proceso
que NO use esta clase puede pisar el archivo. Para nuestro caso de uso (un
único bot con dos comandos mutuamente excluyentes) eso es suficiente.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class LockConflict(RuntimeError):
    """Levantada cuando otro proceso vivo ya tiene el lock."""

    def __init__(self, *, holder: dict[str, object], path: Path) -> None:
        self.holder = holder
        self.path = path
        message = (
            f"lockfile {path} held by pid={holder.get('pid')} "
            f"scope={holder.get('scope')!r} since {holder.get('created_at')}"
        )
        super().__init__(message)


@dataclass
class FileLock:
    """Lock cooperativo por scope (`run` o `mcp-serve`).

    Uso:

        lock = FileLock(Path("data/mcp_serve.lock"), scope="mcp-serve")
        lock.acquire()       # raises LockConflict si otro proceso vivo lo tiene
        try:
            ...
        finally:
            lock.release()
    """

    path: Path
    scope: str
    _acquired: bool = False

    def acquire(self) -> None:
        """Adquiere el lock. Sobrescribe si el holder anterior está muerto."""
        existing = self._read()
        if existing is not None:
            holder_pid = existing.get("pid")
            if isinstance(holder_pid, int) and _pid_is_alive(holder_pid):
                # mismo pid + mismo scope = re-acquire idempotente
                if holder_pid == os.getpid() and existing.get("scope") == self.scope:
                    self._acquired = True
                    return
                raise LockConflict(holder=existing, path=self.path)
            # stale lock: sobreescribir
        self._write()
        self._acquired = True

    def release(self) -> None:
        """Libera el lock si lo poseemos. Idempotente."""
        if not self._acquired:
            return
        try:
            current = self._read()
            if current and current.get("pid") == os.getpid():
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            self._acquired = False

    def is_held(self) -> Optional[dict[str, object]]:
        """Devuelve el contenido del holder vivo o None si no hay lock vigente."""
        existing = self._read()
        if existing is None:
            return None
        pid = existing.get("pid")
        if isinstance(pid, int) and _pid_is_alive(pid):
            return existing
        return None

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _read(self) -> Optional[dict[str, object]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "scope": self.scope,
            "created_at": datetime.now(tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Detección cross-platform de pid vivos
# ---------------------------------------------------------------------------


def _pid_is_alive(pid: int) -> bool:
    """Devuelve True si el pid corresponde a un proceso vivo."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    return _posix_pid_alive(pid)


def _posix_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Existe pero sin permiso → sigue siendo "vivo"
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:  # pragma: no cover - rama Windows real
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # Si algo falla en la detección, asumimos vivo para no liberar locks
        # ajenos por error.
        return True
