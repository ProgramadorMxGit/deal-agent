"""Implementación real de `MaintenanceEnvironment` (disco/systemd/procesos/SQLite).

Toda la interacción con el sistema pasa por un `runner` inyectable
(`(args: list[str], timeout) -> (returncode, stdout)`) para que los tests
puedan simular systemd/pgrep sin tocar el SO real.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, Optional

from ..config import PROJECT_ROOT, get_settings
from ..runtime.profile_lock import ProfileLock
from .nightly import MaintenanceEnvironment

logger = logging.getLogger(__name__)

CommandRunner = Callable[[list], tuple]


def _default_runner(args: list, timeout: Optional[float] = 30.0) -> tuple:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("runner error %s: %s", args, exc)
        return 1, ""


class SystemMaintenanceEnvironment(MaintenanceEnvironment):
    def __init__(
        self,
        *,
        service_name: str = "ofertas-hunter.service",
        runner: Optional[CommandRunner] = None,
        db_path: Optional[Path] = None,
        amazon_lock_path: Optional[str] = None,
        mercadolibre_lock_path: Optional[str] = None,
        orchestrator_pattern: str = "orquestador_ia.py",
        use_sudo: bool = False,
    ) -> None:
        self.service_name = service_name
        self._run = runner or _default_runner
        self.db_path = Path(db_path) if db_path else get_settings().db_path_resolved
        self.amazon_lock_path = amazon_lock_path or str(
            PROJECT_ROOT / "secrets" / "browser_profiles" / "amazon" / ".profile.lock"
        )
        self.mercadolibre_lock_path = mercadolibre_lock_path or str(
            PROJECT_ROOT / "secrets" / "browser_profiles" / "mercadolibre" / ".profile.lock"
        )
        self.orchestrator_pattern = orchestrator_pattern
        # Si el proceso no corre como root (p.ej. el timer corre como
        # `agaetranahoy`), `systemctl stop/start` necesita `sudo -n`.
        self.use_sudo = use_sudo

    def _systemctl(self, *sub: str) -> list:
        base = ["sudo", "-n", "systemctl"] if self.use_sudo else ["systemctl"]
        return base + list(sub)

    # -- disco -------------------------------------------------------
    def disk_free_gb(self) -> float:
        try:
            usage = shutil.disk_usage(str(self.db_path.parent))
            return usage.free / (1024**3)
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk_free_gb error: %s", exc)
            return 0.0

    def db_size_bytes(self) -> int:
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0

    # -- locks / procesos -------------------------------------------
    def _profile_busy(self, lock_path: str) -> bool:
        """Ocupado = no podemos tomar el lock (otro proceso lo tiene)."""
        lock = ProfileLock(lock_path)
        acquired = lock.try_acquire()
        if acquired:
            lock.release()
            return False
        return True

    def amazon_profile_busy(self) -> bool:
        return self._profile_busy(self.amazon_lock_path)

    def mercadolibre_profile_busy(self) -> bool:
        return self._profile_busy(self.mercadolibre_lock_path)

    def bot_chromium_count(self) -> int:
        rc, out = self._run(["pgrep", "-fc", "browser_profiles/"], 10)
        try:
            return int((out or "0").strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0

    def orchestrator_running(self) -> bool:
        rc, out = self._run(["pgrep", "-fc", self.orchestrator_pattern], 10)
        try:
            return int((out or "0").strip().splitlines()[0]) > 0
        except (ValueError, IndexError):
            return False

    # -- systemd -----------------------------------------------------
    def has_systemd_service(self) -> bool:
        """True solo si la unit existe y systemd la cargó (LoadState=loaded).

        `systemctl cat` devuelve rc=0 incluso con units fantasma en algunos
        sistemas; `show --property=LoadState` es la fuente de verdad: una unit
        inexistente reporta `LoadState=not-found`.
        """
        rc, out = self._run(
            ["systemctl", "show", self.service_name, "--property=LoadState"], 10
        )
        if rc != 0:
            return False
        text = (out or "").strip().lower()
        if "loadstate=" not in text:
            return False
        return "loadstate=loaded" in text

    def stop_service(self) -> bool:
        rc, _ = self._run(self._systemctl("stop", self.service_name), 60)
        return rc == 0

    def start_service(self) -> bool:
        rc, _ = self._run(self._systemctl("start", self.service_name), 60)
        return rc == 0

    def reset_failed(self) -> bool:
        rc, _ = self._run(self._systemctl("reset-failed", self.service_name), 15)
        return rc == 0

    def _active_state(self) -> str:
        """Devuelve ActiveState (active|inactive|failed|activating|...) o ''."""
        rc, out = self._run(
            ["systemctl", "show", self.service_name, "--property=ActiveState"], 10
        )
        if rc != 0:
            return ""
        text = (out or "").strip()
        if "=" in text:
            return text.split("=", 1)[1].strip().lower()
        return ""

    def service_is_active(self) -> bool:
        rc, out = self._run(["systemctl", "is-active", self.service_name], 10)
        if (out or "").strip() == "active":
            return True
        # Fallback a ActiveState por si is-active no está disponible.
        return self._active_state() == "active"

    def service_is_failed(self) -> bool:
        rc, out = self._run(["systemctl", "is-failed", self.service_name], 10)
        if (out or "").strip() == "failed":
            return True
        return self._active_state() == "failed"

    # -- SQLite pesado ----------------------------------------------
    def quick_check(self, conn: sqlite3.Connection) -> str:
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            return f"error:{exc}"

    def integrity_check(self, conn: sqlite3.Connection) -> str:
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            return f"error:{exc}"

    def checkpoint(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpoint error: %s", exc)

    def vacuum(self, conn: sqlite3.Connection) -> None:
        conn.execute("VACUUM")


__all__ = ["SystemMaintenanceEnvironment", "CommandRunner"]
