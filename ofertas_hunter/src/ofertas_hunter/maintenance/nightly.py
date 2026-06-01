"""Mantenimiento nocturno seguro de Ofertas Hunter.

Aprovecha la ventana de hibernación (cuando el bot NO publica) para:

1. Detectar ventana nocturna segura (horario + locks + procesos).
2. Detener el servicio principal (si está en systemd).
3. Purgar `runtime_events` verbosos (`mcp_tool_called` > N horas) en batches.
4. Checkpoint WAL + `VACUUM` (solo si hay espacio y quick_check OK).
5. `integrity_check`. Si falla, NO reinicia.
6. Reiniciar el servicio principal (si corresponde).
7. Emitir `nightly_maintenance_summary` en `runtime_events`.

NO toca: outbox, published_messages, frontier, products, offers,
discarded_candidates, gates, cookies ni perfiles. NO publica nada.

El runner recibe un `MaintenanceEnvironment` inyectable para que toda la
interacción con disco/procesos/servicio/SQLite-pesado sea testeable sin
systemd ni VPS reales.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kinds de runtime_events que NUNCA se borran (alto valor forense).
# El resto de la purga solo toca `mcp_tool_called`, así que esta lista es
# defensiva/documental para futuras políticas de "verbose > N días".
# ---------------------------------------------------------------------------
IMPORTANT_EVENT_KINDS = frozenset(
    {
        "amazon_legacy_captcha_suspect",
        "amazon_captcha_confirmed",
        "cookie_expiry",
        "false_price_error_detected",
        "ml_session_state_changed",
        "ml_session_admin_alerted",
        "ml_session_active_validation",
        "ml_cookies_reloaded",
        "ml_cookies_promoted",
        "ml_cookies_validated",
        "ml_session_context_rotated",
        "orchestrator_starting",
        "orchestrator_stopping",
        "agent_restart",
        "diversity_curator_decision",
        "nightly_maintenance_summary",
    }
)

# El único kind verboso que la purga conservadora elimina por edad.
VERBOSE_PURGE_KIND = "mcp_tool_called"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_hhmm(value: str, default: time) -> time:
    if not value:
        return default
    try:
        hh, mm = str(value).strip().split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        logger.warning("nightly: hora inválida %r, uso %s", value, default)
        return default


def is_within_window(now: datetime, start: time, end: time) -> bool:
    """True si `now` (tz-aware) está en [start, end). Soporta cruce de medianoche."""
    t = now.timetz().replace(tzinfo=None)
    t = time(t.hour, t.minute, t.second)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightlyMaintenanceConfig:
    enabled: bool = True
    start: time = time(3, 0)
    end: time = time(4, 30)
    timezone_name: str = "America/Mexico_City"
    keep_mcp_tool_called_hours: int = 48
    keep_verbose_days: int = 7
    vacuum_enabled: bool = True
    vacuum_min_free_gb: float = 10.0
    batch_size: int = 100000
    restart_after: bool = True
    max_seconds: int = 3600
    create_backup: bool = False
    backup_retention: int = 1

    @classmethod
    def from_settings(cls, s) -> "NightlyMaintenanceConfig":
        return cls(
            enabled=bool(getattr(s, "nightly_maintenance_enabled", True)),
            start=_parse_hhmm(getattr(s, "nightly_maintenance_start", "03:00"), time(3, 0)),
            end=_parse_hhmm(getattr(s, "nightly_maintenance_end", "04:30"), time(4, 30)),
            timezone_name=getattr(s, "nightly_maintenance_timezone", "America/Mexico_City"),
            keep_mcp_tool_called_hours=int(getattr(s, "runtime_events_keep_mcp_tool_called_hours", 48)),
            keep_verbose_days=int(getattr(s, "runtime_events_keep_verbose_days", 7)),
            vacuum_enabled=bool(getattr(s, "nightly_vacuum_enabled", True)),
            vacuum_min_free_gb=float(getattr(s, "nightly_vacuum_min_free_gb", 10.0)),
            batch_size=int(getattr(s, "nightly_maintenance_batch_size", 100000)),
            restart_after=bool(getattr(s, "nightly_maintenance_restart_after", True)),
            max_seconds=int(getattr(s, "nightly_maintenance_max_seconds", 3600)),
            create_backup=bool(getattr(s, "nightly_maintenance_create_backup", False)),
            backup_retention=int(getattr(s, "nightly_maintenance_backup_retention", 1)),
        )


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------


@dataclass
class PurgeReport:
    rows_before: int = 0
    rows_candidate: int = 0
    rows_deleted: int = 0
    rows_after: int = 0
    batches: int = 0
    duration_seconds: float = 0.0
    dry_run: bool = False


@dataclass
class MaintenanceSummary:
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    safe_window: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    dry_run: bool = False
    success: bool = False
    db_size_before: int = 0
    db_size_after: int = 0
    disk_free_gb_before: float = 0.0
    disk_free_gb_after: float = 0.0
    runtime_events_before: int = 0
    runtime_events_after: int = 0
    rows_candidate: int = 0
    rows_deleted: int = 0
    purge_batches: int = 0
    quick_check: Optional[str] = None
    integrity_check: Optional[str] = None
    vacuum_done: bool = False
    vacuum_skipped_reason: Optional[str] = None
    service_stopped: bool = False
    service_started: bool = False
    restart_done: bool = False
    backup_created: bool = False
    outbox_counts_before: dict = field(default_factory=dict)
    outbox_counts_after: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# Purga (función pura sobre una conexión)
# ---------------------------------------------------------------------------


def purge_runtime_events(
    conn: sqlite3.Connection,
    *,
    now: Optional[datetime] = None,
    keep_mcp_hours: int = 48,
    batch_size: int = 100000,
    dry_run: bool = False,
) -> PurgeReport:
    """Borra `mcp_tool_called` con created_at < now-keep_mcp_hours, en batches.

    No toca otros kinds ni otras tablas. En dry-run solo cuenta.
    """
    now = now or _now_utc()
    cutoff = _iso(now - timedelta(hours=keep_mcp_hours))
    report = PurgeReport(dry_run=dry_run)
    t0 = _time.time()

    report.rows_before = conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
    report.rows_candidate = conn.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE kind=? AND created_at < ?",
        (VERBOSE_PURGE_KIND, cutoff),
    ).fetchone()[0]

    if dry_run:
        report.rows_after = report.rows_before
        report.duration_seconds = _time.time() - t0
        return report

    deleted = 0
    batches = 0
    bs = max(1, int(batch_size))
    while True:
        cur = conn.execute(
            "DELETE FROM runtime_events WHERE rowid IN ("
            "  SELECT rowid FROM runtime_events "
            "  WHERE kind=? AND created_at < ? LIMIT ?)",
            (VERBOSE_PURGE_KIND, cutoff, bs),
        )
        n = cur.rowcount or 0
        conn.commit()
        if n == 0:
            break
        deleted += n
        batches += 1

    report.rows_deleted = deleted
    report.batches = batches
    report.rows_after = conn.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
    report.duration_seconds = _time.time() - t0
    return report


def _outbox_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT state, COUNT(*) c FROM outbox GROUP BY state").fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Environment inyectable (disco / procesos / servicio / SQLite pesado)
# ---------------------------------------------------------------------------


class MaintenanceEnvironment:
    """Interfaz de efectos colaterales. La implementación real está abajo;
    los tests inyectan un fake."""

    def disk_free_gb(self) -> float: raise NotImplementedError
    def db_size_bytes(self) -> int: raise NotImplementedError
    def amazon_profile_busy(self) -> bool: raise NotImplementedError
    def mercadolibre_profile_busy(self) -> bool: raise NotImplementedError
    def bot_chromium_count(self) -> int: raise NotImplementedError
    def orchestrator_running(self) -> bool: raise NotImplementedError
    def has_systemd_service(self) -> bool: raise NotImplementedError
    def stop_service(self) -> bool: raise NotImplementedError
    def start_service(self) -> bool: raise NotImplementedError
    def quick_check(self, conn: sqlite3.Connection) -> str: raise NotImplementedError
    def integrity_check(self, conn: sqlite3.Connection) -> str: raise NotImplementedError
    def vacuum(self, conn: sqlite3.Connection) -> None: raise NotImplementedError
    def checkpoint(self, conn: sqlite3.Connection) -> None: raise NotImplementedError


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class NightlyMaintenanceRunner:
    def __init__(
        self,
        *,
        db_path: Path,
        config: NightlyMaintenanceConfig,
        env: MaintenanceEnvironment,
        clock: Callable[[], datetime] = _now_utc,
        force_window: bool = False,
        dry_run: bool = False,
        skip_vacuum: bool = False,
        no_restart: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self.env = env
        self._clock = clock
        self.force_window = force_window
        self.dry_run = dry_run
        self.skip_vacuum = skip_vacuum
        self.no_restart = no_restart

    # ------------------------------------------------------------------

    def _is_safe_window(self, summary: MaintenanceSummary) -> bool:
        if self.force_window:
            return True
        if not self.config.enabled:
            summary.warnings.append("disabled")
            return False
        now = self._clock()
        if not is_within_window(now, self.config.start, self.config.end):
            return False
        # Locks / procesos críticos: si hay perfil ocupado o chromium del bot
        # vivo, NO es seguro (en producción el servicio aún corre; lo
        # detendremos después, pero un lock activo indica operación en curso).
        if self.env.amazon_profile_busy() or self.env.mercadolibre_profile_busy():
            return False
        return True

    def run(self) -> MaintenanceSummary:
        summary = MaintenanceSummary()
        t0 = _time.time()
        summary.started_at = _iso(self._clock())

        # 1. Ventana segura.
        if not self._is_safe_window(summary):
            summary.skipped = True
            summary.skip_reason = "not_safe_window"
            summary.finished_at = _iso(self._clock())
            summary.duration_seconds = _time.time() - t0
            logger.warning("nightly_maintenance_skipped_not_safe")
            self._emit_summary(summary)
            return summary
        summary.safe_window = True
        summary.dry_run = self.dry_run

        # 2. Métricas iniciales + quick_check (sobre la DB activa, read-only).
        summary.disk_free_gb_before = self.env.disk_free_gb()
        summary.db_size_before = self.env.db_size_bytes()

        conn = self._connect()
        try:
            summary.outbox_counts_before = _outbox_counts(conn)
            summary.runtime_events_before = conn.execute(
                "SELECT COUNT(*) FROM runtime_events"
            ).fetchone()[0]
            qc = self.env.quick_check(conn)
            summary.quick_check = qc
            if qc != "ok":
                summary.success = False
                summary.errors.append(f"quick_check:{qc}")
                logger.error("nightly_maintenance quick_check FALLÓ: %s", qc)
                self._finish(summary, t0, conn)
                return summary
        finally:
            conn.close()

        # 3. Dry-run: solo contar candidatos, sin tocar servicio/DB.
        if self.dry_run:
            conn = self._connect()
            try:
                pr = purge_runtime_events(
                    conn,
                    now=self._clock(),
                    keep_mcp_hours=self.config.keep_mcp_tool_called_hours,
                    batch_size=self.config.batch_size,
                    dry_run=True,
                )
            finally:
                conn.close()
            summary.rows_candidate = pr.rows_candidate
            summary.rows_deleted = 0
            summary.runtime_events_after = summary.runtime_events_before
            summary.outbox_counts_after = dict(summary.outbox_counts_before)
            summary.disk_free_gb_after = summary.disk_free_gb_before
            summary.db_size_after = summary.db_size_before
            summary.success = True
            self._finish(summary, t0, None, emit=True)
            return summary

        # 4. Detener servicio principal (acceso exclusivo para VACUUM).
        if self.env.has_systemd_service() and self.env.orchestrator_running():
            summary.service_stopped = self.env.stop_service()
            if not summary.service_stopped:
                summary.errors.append("stop_service_failed")
                summary.success = False
                self._finish(summary, t0, None, emit=True)
                return summary

        # 5. Confirmar que no quedan chromiums del bot.
        if self.env.bot_chromium_count() > 0:
            summary.warnings.append("bot_chromium_still_alive")

        # 6. Purga real.
        conn = self._connect()
        try:
            pr = purge_runtime_events(
                conn,
                now=self._clock(),
                keep_mcp_hours=self.config.keep_mcp_tool_called_hours,
                batch_size=self.config.batch_size,
                dry_run=False,
            )
            summary.rows_candidate = pr.rows_candidate
            summary.rows_deleted = pr.rows_deleted
            summary.purge_batches = pr.batches

            # 7. VACUUM (con todas las salvaguardas).
            self._maybe_vacuum(summary, conn)

            summary.runtime_events_after = conn.execute(
                "SELECT COUNT(*) FROM runtime_events"
            ).fetchone()[0]
            summary.outbox_counts_after = _outbox_counts(conn)
        finally:
            conn.close()

        summary.disk_free_gb_after = self.env.disk_free_gb()
        summary.db_size_after = self.env.db_size_bytes()

        # 8. Decidir éxito (integrity gate) y reinicio.
        integrity_failed = summary.integrity_check is not None and summary.integrity_check != "ok"
        summary.success = (not integrity_failed) and not summary.errors

        if integrity_failed:
            summary.errors.append(f"integrity_check:{summary.integrity_check}")
            logger.error("nightly_maintenance integrity_check FALLÓ — NO se reinicia")
            self._finish(summary, t0, None, emit=True)
            return summary

        # 9. Reinicio. Garantiza que el servicio quede ARRIBA al terminar:
        #    se arranca si lo detuvimos nosotros, o si estaba caído de antes
        #    (el operador espera el bot vivo tras el mantenimiento). No se
        #    arranca con --no-restart ni si no hay unit systemd.
        should_restart = (
            self.config.restart_after
            and not self.no_restart
            and self.env.has_systemd_service()
            and not self.env.orchestrator_running()
        )
        if should_restart:
            summary.service_started = self.env.start_service()
            summary.restart_done = summary.service_started
            if not summary.service_started:
                summary.errors.append("restart_failed")
                summary.success = False
                logger.error("nightly_restart_failed")

        self._finish(summary, t0, None, emit=True)
        return summary

    # ------------------------------------------------------------------

    def _maybe_vacuum(self, summary: MaintenanceSummary, conn: sqlite3.Connection) -> None:
        if self.skip_vacuum:
            summary.vacuum_skipped_reason = "flag"
            return
        if not self.config.vacuum_enabled:
            summary.vacuum_skipped_reason = "disabled"
            return
        # Sin servicio systemd que detener y con el orquestador aún vivo, un
        # VACUUM competiría por el lock exclusivo de SQLite con el bot
        # escribiendo. La purga (batches, no exclusiva) sí es segura, pero el
        # VACUUM se salta hasta que el operador detenga el bot.
        if (
            not summary.service_stopped
            and not self.env.has_systemd_service()
            and self.env.orchestrator_running()
        ):
            summary.vacuum_skipped_reason = "no_systemd_orchestrator_running"
            logger.warning("nightly_vacuum_skipped: sin systemd y orquestador vivo")
            return
        if self.env.disk_free_gb() < self.config.vacuum_min_free_gb:
            summary.vacuum_skipped_reason = "low_disk"
            logger.warning("nightly_vacuum_skipped_low_disk")
            return
        try:
            self.env.checkpoint(conn)
            self.env.vacuum(conn)
            summary.vacuum_done = True
            summary.integrity_check = self.env.integrity_check(conn)
        except Exception as exc:  # noqa: BLE001
            summary.vacuum_skipped_reason = "vacuum_error"
            summary.errors.append(f"vacuum_error:{exc}")
            logger.error("nightly_vacuum falló: %s", exc)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=60.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def _finish(
        self,
        summary: MaintenanceSummary,
        t0: float,
        conn: Optional[sqlite3.Connection],
        *,
        emit: bool = True,
    ) -> None:
        summary.finished_at = _iso(self._clock())
        summary.duration_seconds = round(_time.time() - t0, 3)
        if emit:
            self._emit_summary(summary)

    def _emit_summary(self, summary: MaintenanceSummary) -> None:
        """Persiste el resumen en runtime_events (best-effort).

        En dry-run NO se escribe nada (cero efectos colaterales): el resumen
        igualmente se devuelve en memoria y lo imprime el CLI.
        """
        if self.dry_run:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "nightly_maintenance_summary",
                        "error" if (summary.errors or not summary.success and not summary.skipped) else "info",
                        json.dumps(summary.as_dict(), ensure_ascii=False),
                        _iso(self._clock()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("nightly: no se pudo emitir summary: %s", exc)


__all__ = [
    "NightlyMaintenanceConfig",
    "NightlyMaintenanceRunner",
    "MaintenanceEnvironment",
    "MaintenanceSummary",
    "PurgeReport",
    "purge_runtime_events",
    "is_within_window",
    "IMPORTANT_EVENT_KINDS",
]
