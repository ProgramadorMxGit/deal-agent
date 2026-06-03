"""Tests del runner de nightly_maintenance (orquestación + safety)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone
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


class FakeEnv(MaintenanceEnvironment):
    """Entorno simulado: controla disco, locks, procesos y servicio."""

    def __init__(self, *, free_gb=50.0, db_size=1.0, profiles_busy=False,
                 chromium_running=False, orchestrator_running=True,
                 quick_ok=True, integrity_ok=True, vacuum_raises=False):
        self._free_gb = free_gb
        self._db_size = db_size
        self._profiles_busy = profiles_busy
        self._chromium = chromium_running
        self._orch = orchestrator_running
        self._quick_ok = quick_ok
        self._integrity_ok = integrity_ok
        self._vacuum_raises = vacuum_raises
        self.calls = []

    # disco
    def disk_free_gb(self): return self._free_gb
    def db_size_bytes(self): return int(self._db_size * 1024**3)
    # procesos / locks
    def amazon_profile_busy(self): return self._profiles_busy
    def mercadolibre_profile_busy(self): return self._profiles_busy
    def bot_chromium_count(self): return 1 if self._chromium else 0
    def orchestrator_running(self): return self._orch
    # servicio
    def has_systemd_service(self): return True
    def stop_service(self):
        self.calls.append("stop"); self._orch = False; self._chromium = False; return True
    def start_service(self):
        self.calls.append("start"); self._orch = True; return True
    # db checks
    def quick_check(self, conn): return "ok" if self._quick_ok else "corrupt"
    def integrity_check(self, conn): return "ok" if self._integrity_ok else "bad"
    def vacuum(self, conn):
        self.calls.append("vacuum")
        if self._vacuum_raises:
            raise sqlite3.OperationalError("vacuum failed")
    def checkpoint(self, conn): self.calls.append("checkpoint")


def _runner(db_path, env, *, force_window=False, dry_run=False, now=None,
            skip_vacuum=False, no_restart=False, cfg=None):
    config = cfg or NightlyMaintenanceConfig(
        start=time(3, 0), end=time(4, 30), timezone_name="UTC",
    )
    now = now or datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    return NightlyMaintenanceRunner(
        db_path=db_path,
        config=config,
        env=env,
        clock=lambda: now,
        force_window=force_window,
        dry_run=dry_run,
        skip_vacuum=skip_vacuum,
        no_restart=no_restart,
    )


def _seed_old_events(db_path, now, n=1500):
    c = connect(db_path)
    old = _iso(now - timedelta(hours=72))
    for _ in range(n):
        c.execute("INSERT INTO runtime_events (kind, severity, payload_json, created_at) "
                  "VALUES ('mcp_tool_called','info','{}',?)", (old,))
    c.commit(); c.close()


# D) Fuera de ventana → no corre
def test_skips_when_outside_window(db_path):
    env = FakeEnv()
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)  # mediodía
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.skipped is True
    assert summary.skip_reason == "not_safe_window"
    assert "stop" not in env.calls
    assert summary.vacuum_done is False


# E) --force-window permite correr fuera de ventana
def test_force_window_allows_run(db_path):
    env = FakeEnv()
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    runner = _runner(db_path, env, now=now, force_window=True)
    summary = runner.run()
    assert summary.skipped is False
    assert summary.safe_window is True


# A) dry-run no borra ni detiene servicio ni hace vacuum
def test_dry_run_no_side_effects(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 1200)
    env = FakeEnv()
    runner = _runner(db_path, env, now=now, dry_run=True)
    summary = runner.run()
    assert summary.dry_run is True
    assert summary.rows_deleted == 0
    assert summary.rows_candidate == 1200
    assert "stop" not in env.calls
    assert "vacuum" not in env.calls
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 1200
    c.close()


# F) lock de perfil activo → aborta antes de tocar nada
def test_aborts_when_profile_locked(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    env = FakeEnv(profiles_busy=True)
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.skipped is True
    assert summary.skip_reason == "not_safe_window"
    assert "vacuum" not in env.calls


# G) sin espacio → corre purga pero salta VACUUM
def test_skips_vacuum_when_low_disk(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 1200)
    env = FakeEnv(free_gb=3.0)  # < min_free_gb 10
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.rows_deleted == 1200
    assert summary.vacuum_done is False
    assert summary.vacuum_skipped_reason == "low_disk"
    assert "vacuum" not in env.calls
    # purga sí ocurrió → servicio se reinició
    assert "start" in env.calls


# H) quick_check falla → aborta antes de purgar
def test_aborts_when_quick_check_fails(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 800)
    env = FakeEnv(quick_ok=False)
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.success is False
    assert summary.quick_check == "corrupt"
    assert summary.rows_deleted == 0
    assert "vacuum" not in env.calls


# I) integrity_check falla tras VACUUM → no reinicia normal
def test_no_restart_when_integrity_fails(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 1200)
    env = FakeEnv(integrity_ok=False)
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.integrity_check == "bad"
    assert summary.success is False
    assert summary.restart_done is False
    assert "start" not in env.calls


# J) todo OK → éxito + restart
def test_full_success_with_restart(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 1200)
    env = FakeEnv()
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.success is True
    assert summary.rows_deleted == 1200
    assert summary.vacuum_done is True
    assert summary.integrity_check == "ok"
    assert summary.restart_done is True
    assert env.calls == ["stop", "checkpoint", "vacuum", "start"] or "start" in env.calls


# M) no_restart → no reinicia aunque todo OK
def test_no_restart_flag(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 600)
    env = FakeEnv()
    runner = _runner(db_path, env, now=now, no_restart=True)
    summary = runner.run()
    assert summary.success is True
    assert summary.restart_done is False
    assert "start" not in env.calls


# Restart-mode con el servicio YA detenido al inicio → debe ARRANCARLO al
# final (el operador espera el bot arriba tras el mantenimiento).
def test_restart_starts_service_even_if_already_stopped(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 500)
    env = FakeEnv(orchestrator_running=False)  # systemd existe, bot caído
    runner = _runner(db_path, env, now=now)  # restart_after default True
    summary = runner.run()
    assert summary.success is True
    # no se detuvo (ya estaba abajo) pero SÍ se arrancó
    assert summary.service_stopped is False
    assert summary.service_started is True
    assert summary.restart_done is True
    assert "start" in env.calls


# skip_vacuum flag
def test_skip_vacuum_flag(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 600)
    env = FakeEnv()
    runner = _runner(db_path, env, now=now, skip_vacuum=True)
    summary = runner.run()
    assert summary.vacuum_done is False
    assert summary.vacuum_skipped_reason == "flag"
    assert "vacuum" not in env.calls


# N) outbox no cambia + summary event emitido
def test_outbox_untouched_and_summary_emitted(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 300)
    c = connect(db_path)
    # un item de outbox (con product+offer FK)
    c.execute("INSERT INTO products (marketplace, marketplace_id, url_canonical, title, "
              "first_seen_at, last_seen_at) VALUES ('amazon','A','https://a','t',?,?)",
              (_iso(now), _iso(now)))
    pid = c.execute("SELECT id FROM products").fetchone()[0]
    c.execute("INSERT INTO offers (product_id, classification, score, reasons_json, "
              "discount_percent, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
              (pid, "x", 0, "[]", None, "eligible", _iso(now), _iso(now)))
    oid = c.execute("SELECT id FROM offers").fetchone()[0]
    c.execute("INSERT INTO outbox (offer_id, type, enqueued_at, scheduled_for, attempts, "
              "last_attempt_at, state, message_payload_json) VALUES (?,?,?,?,?,?,?,?)",
              (oid, "normal", _iso(now), None, 0, None, "pending", "{}"))
    c.commit()
    outbox_before = c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    c.close()

    env = FakeEnv()
    runner = _runner(db_path, env, now=now)
    summary = runner.run()

    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == outbox_before
    # summary event persistido
    n = c.execute("SELECT COUNT(*) FROM runtime_events WHERE kind='nightly_maintenance_summary'").fetchone()[0]
    assert n == 1
    c.close()
    assert summary.outbox_counts_before == summary.outbox_counts_after


# L) idempotencia: correr dos veces seguidas no rompe
def test_idempotent_second_run(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 400)
    env = FakeEnv()
    r1 = _runner(db_path, env, now=now).run()
    assert r1.success is True
    assert r1.rows_deleted == 400
    # segunda corrida: ya no hay viejos
    env2 = FakeEnv()
    r2 = _runner(db_path, env2, now=now).run()
    assert r2.success is True
    assert r2.rows_deleted == 0


# Sin systemd + orquestador vivo → purga sí, pero VACUUM se salta (evita
# contención de lock con el bot escribiendo).
class FakeEnvNoSystemd(FakeEnv):
    def has_systemd_service(self):
        return False


def test_no_systemd_orchestrator_running_skips_vacuum(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 700)
    env = FakeEnvNoSystemd(orchestrator_running=True)
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.rows_deleted == 700
    assert "stop" not in env.calls
    assert summary.service_stopped is False
    assert summary.restart_done is False
    assert summary.vacuum_done is False
    assert summary.vacuum_skipped_reason == "orchestrator_running_no_exclusive_access"
    assert "vacuum" not in env.calls
    assert summary.success is True


def test_no_systemd_orchestrator_stopped_allows_vacuum(db_path):
    now = datetime(2026, 5, 30, 3, 30, tzinfo=timezone.utc)
    _seed_old_events(db_path, now, 700)
    env = FakeEnvNoSystemd(orchestrator_running=False)
    runner = _runner(db_path, env, now=now)
    summary = runner.run()
    assert summary.rows_deleted == 700
    assert summary.vacuum_done is True
    assert summary.integrity_check == "ok"
    assert summary.success is True
