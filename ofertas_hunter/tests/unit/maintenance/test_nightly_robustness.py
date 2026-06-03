"""Tests de robustez del nightly-maintenance (Task 4/5).

Cubre:
F) nightly NO corre VACUUM si el stop del servicio falla.
G) nightly hace reset-failed antes de start si el service quedó failed.
H) nightly reintenta start una vez.
I) summary incluye service_final_state.
J) service final active → success.
K) service final failed → error.
+ eventos granulares emitidos.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.maintenance.nightly import (
    MaintenanceEnvironment,
    NightlyMaintenanceConfig,
    NightlyMaintenanceRunner,
)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def db_path(tmp_path: Path):
    init_db(tmp_path / "x.db")
    return tmp_path / "x.db"


class RobustEnv(MaintenanceEnvironment):
    """Entorno con control fino de stop/start/failed/active para robustez."""

    def __init__(self, *, orchestrator_running=True, stop_succeeds=True,
                 stop_truly_stops=True, start_attempts_to_succeed=1,
                 starts_failed_after_stop_fail=True, integrity_ok=True):
        self._orch = orchestrator_running
        self._stop_succeeds = stop_succeeds
        self._stop_truly_stops = stop_truly_stops
        # nº de intento de start en el que empieza a funcionar (1 = primero)
        self._start_success_on_attempt = start_attempts_to_succeed
        self._start_calls = 0
        self._failed = False
        self._starts_failed_after_stop_fail = starts_failed_after_stop_fail
        self._integrity_ok = integrity_ok
        self.calls = []
        self.events = []

    # disco / db
    def disk_free_gb(self): return 50.0
    def db_size_bytes(self): return 1024**3
    def amazon_profile_busy(self): return False
    def mercadolibre_profile_busy(self): return False
    def bot_chromium_count(self): return 0
    def orchestrator_running(self): return self._orch
    def has_systemd_service(self): return True

    def stop_service(self):
        self.calls.append("stop")
        if not self._stop_succeeds:
            # stop devolvió error y el servicio quedó failed.
            if self._starts_failed_after_stop_fail:
                self._failed = True
            return False
        if self._stop_truly_stops:
            self._orch = False
        return True

    def start_service(self):
        self._start_calls += 1
        self.calls.append("start")
        if self._start_calls >= self._start_success_on_attempt:
            self._orch = True
            self._failed = False
            return True
        # arranque falla este intento
        self._failed = True
        return False

    def reset_failed(self):
        self.calls.append("reset_failed")
        self._failed = False
        return True

    def service_is_active(self): return self._orch
    def service_is_failed(self): return self._failed

    def quick_check(self, conn): return "ok"
    def integrity_check(self, conn): return "ok" if self._integrity_ok else "bad"
    def vacuum(self, conn): self.calls.append("vacuum")
    def checkpoint(self, conn): self.calls.append("checkpoint")


def _runner(db_path, env, **kw):
    cfg = NightlyMaintenanceConfig(start=time(3, 0), end=time(4, 30), timezone_name="UTC")
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    return NightlyMaintenanceRunner(
        db_path=db_path, config=cfg, env=env, clock=lambda: now,
        force_window=kw.pop("force_window", False), **kw,
    )


def _events(db_path, kind=None):
    c = connect(db_path)
    if kind:
        rows = c.execute("SELECT kind FROM runtime_events WHERE kind=?", (kind,)).fetchall()
    else:
        rows = c.execute("SELECT kind FROM runtime_events").fetchall()
    c.close()
    return [r[0] for r in rows]


# F) stop falla (p.ej. bot en foreground) → PURGA igual, VACUUM se salta.
#    La purga es segura en DB viva (WAL); solo el VACUUM requiere exclusividad.
def test_F_stop_fail_skips_vacuum(db_path):
    env = RobustEnv(stop_succeeds=False)
    summary = _runner(db_path, env).run()
    assert summary.service_stopped is False
    assert summary.service_stop_result == "stop_failed_continue_purge"
    # VACUUM se salta porque el orquestador sigue vivo (sin acceso exclusivo).
    assert summary.vacuum_done is False
    assert "vacuum" not in env.calls
    assert summary.vacuum_skipped_reason == "orchestrator_running_no_exclusive_access"
    # La purga SÍ corre (no se aborta como antes).
    assert summary.rows_deleted >= 0
    kinds = _events(db_path)
    assert "nightly_service_stop_failed" in kinds


# F-bis) stop falla pero igual intenta dejar el servicio ARRIBA
def test_F_stop_fail_still_tries_to_start(db_path):
    # stop falla y deja failed; el servicio sigue 'running' (timeout→kill no
    # garantizado). Para este caso simulamos que el bot quedó caído.
    env = RobustEnv(stop_succeeds=False, orchestrator_running=False)
    summary = _runner(db_path, env).run()
    # reset-failed + start se intentan
    assert "start" in env.calls
    # como start tiene éxito al primer intento, queda activo
    assert summary.service_final_state == "active"


# G) servicio failed tras stop → reset-failed antes de start
def test_G_reset_failed_before_start(db_path):
    # bot caído + en estado failed al inicio; restart debe reset-failed→start
    env = RobustEnv(orchestrator_running=False)
    env._failed = True
    summary = _runner(db_path, env).run()
    assert "reset_failed" in env.calls
    # reset_failed ocurre antes del primer start
    assert env.calls.index("reset_failed") < env.calls.index("start")
    assert summary.reset_failed_done is True
    assert summary.service_started is True


# H) start falla la 1a vez, reintenta y queda activo
def test_H_start_retried_once(db_path):
    env = RobustEnv(orchestrator_running=False, start_attempts_to_succeed=2)
    summary = _runner(db_path, env).run()
    assert summary.start_retried is True
    assert env.calls.count("start") == 2
    assert summary.service_started is True
    assert summary.service_final_state == "active"
    assert "nightly_service_start_retried" in _events(db_path)


# I + J) summary incluye final_state y active → success
def test_I_J_final_state_active_success(db_path):
    env = RobustEnv()
    summary = _runner(db_path, env).run()
    assert summary.service_final_state == "active"
    assert summary.success is True
    assert "nightly_service_final_state" in _events(db_path)


# K) start nunca funciona → final failed → error
def test_K_final_state_failed_marks_error(db_path):
    env = RobustEnv(orchestrator_running=False, start_attempts_to_succeed=99)
    summary = _runner(db_path, env).run()
    assert summary.service_started is False
    assert summary.service_final_state in ("failed", "inactive")
    assert summary.success is False
    assert "nightly_service_start_failed" in _events(db_path)


# granular stop events on success path
def test_stop_events_on_success(db_path):
    env = RobustEnv()
    _runner(db_path, env).run()
    kinds = _events(db_path)
    assert "nightly_service_stop_started" in kinds
    assert "nightly_service_stop_done" in kinds
    assert "nightly_service_start_done" in kinds
