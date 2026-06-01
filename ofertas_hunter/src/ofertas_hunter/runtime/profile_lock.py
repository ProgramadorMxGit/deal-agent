"""Lock de archivo cross-process para perfiles de navegador persistentes.

Evita que dos procesos (orquestador + CLI, o dos loops del mismo proceso)
lancen `launch_persistent_context` sobre el MISMO `user_data_dir` de
Chromium, lo que corrompe el perfil y produce `sitestripe_not_visible` /
`TargetClosedError`.

Implementación:
- POSIX: `fcntl.flock` con `LOCK_EX | LOCK_NB` (no bloqueante). El lock se
  libera automáticamente si el proceso muere (lo libera el SO al cerrar el
  descriptor), evitando locks huérfanos.
- Sin `fcntl` (Windows, solo para tests locales): degradación a un lock
  best-effort basado en existencia de archivo. No es robusto cross-process
  en Windows, pero el runtime real corre en Linux.

Uso:
    lock = ProfileLock("secrets/browser_profiles/amazon/.profile.lock")
    if lock.try_acquire():
        try:
            ...  # abrir Chromium con el perfil
        finally:
            lock.release()

o como context manager:
    with ProfileLock(path) as acquired:
        if not acquired:
            return  # ocupado: saltar y reintentar luego
        ...
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import fcntl  # type: ignore

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore
    _HAS_FCNTL = False


class ProfileLock:
    """Lock no bloqueante sobre un archivo, para serializar uso de un perfil."""

    def __init__(self, lock_path: str) -> None:
        self.lock_path = str(lock_path)
        self._fd: Optional[int] = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def try_acquire(self) -> bool:
        """Intenta tomar el lock sin bloquear. True si se obtuvo."""
        if self._held:
            return True
        # Asegurar que el directorio del lock existe.
        try:
            Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("profile_lock: no se pudo crear dir del lock: %s", exc)

        if _HAS_FCNTL:
            return self._acquire_fcntl()
        return self._acquire_fallback()

    def _acquire_fcntl(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("profile_lock: open falló: %s", exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(fd)
            return False
        # Escribir PID para diagnóstico (no es la fuente de verdad del lock).
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except Exception:  # pragma: no cover - diagnóstico best-effort
            pass
        self._fd = fd
        self._held = True
        return True

    def _acquire_fallback(self) -> bool:  # pragma: no cover - Windows tests
        # Best-effort: O_CREAT | O_EXCL falla si el archivo ya existe.
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            return False
        except Exception as exc:
            logger.warning("profile_lock(fallback): open falló: %s", exc)
            return False
        try:
            os.write(fd, str(os.getpid()).encode())
        except Exception:
            pass
        self._fd = fd
        self._held = True
        return True

    def release(self) -> None:
        """Libera el lock. Idempotente: no rompe si ya estaba liberado."""
        if not self._held and self._fd is None:
            return
        fd = self._fd
        self._fd = None
        self._held = False
        if fd is None:
            return
        try:
            if _HAS_FCNTL:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:  # pragma: no cover - defensivo
                    pass
            os.close(fd)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("profile_lock: release falló: %s", exc)
        # En el fallback (Windows) borramos el archivo para liberar.
        if not _HAS_FCNTL:  # pragma: no cover - Windows
            try:
                os.unlink(self.lock_path)
            except Exception:
                pass

    def __enter__(self) -> bool:
        return self.try_acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
