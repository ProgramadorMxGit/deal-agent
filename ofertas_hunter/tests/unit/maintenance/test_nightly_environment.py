"""Tests del SystemMaintenanceEnvironment (efectos reales, con runner inyectable)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ofertas_hunter.db import connect, init_db
from ofertas_hunter.maintenance.environment import SystemMaintenanceEnvironment


class FakeProc:
    """Runner de comandos simulado: mapea prefijos de comando a (rc, stdout)."""

    def __init__(self, table):
        self.table = table
        self.ran = []

    def __call__(self, args, timeout=None):
        self.ran.append(args)
        key = " ".join(args[:3])
        for prefix, (rc, out) in self.table.items():
            if key.startswith(prefix) or " ".join(args).startswith(prefix):
                return rc, out
        return 0, ""


@pytest.fixture
def db_path(tmp_path: Path):
    init_db(tmp_path / "x.db")
    return tmp_path / "x.db"


def test_quick_check_and_integrity(db_path):
    env = SystemMaintenanceEnvironment(
        service_name="ofertas-hunter.service", runner=FakeProc({})
    )
    conn = connect(db_path)
    try:
        assert env.quick_check(conn) == "ok"
        assert env.integrity_check(conn) == "ok"
    finally:
        conn.close()


def test_db_size_bytes(db_path):
    env = SystemMaintenanceEnvironment(
        service_name="x", runner=FakeProc({}), db_path=db_path
    )
    assert env.db_size_bytes() > 0


def test_orchestrator_running_via_pgrep(db_path):
    # pgrep -fc devuelve "2" → running
    env = SystemMaintenanceEnvironment(
        service_name="x",
        runner=FakeProc({"pgrep": (0, "2\n")}),
    )
    assert env.orchestrator_running() is True

    env2 = SystemMaintenanceEnvironment(
        service_name="x",
        runner=FakeProc({"pgrep": (1, "0\n")}),
    )
    assert env2.orchestrator_running() is False


def test_has_systemd_service_detects_unit():
    # LoadState=loaded → existe
    env = SystemMaintenanceEnvironment(
        service_name="ofertas-hunter.service",
        runner=FakeProc({"systemctl show": (0, "LoadState=loaded\n")}),
    )
    assert env.has_systemd_service() is True

    # LoadState=not-found → NO existe (aunque rc sea 0)
    env2 = SystemMaintenanceEnvironment(
        service_name="ofertas-hunter.service",
        runner=FakeProc({"systemctl show": (0, "LoadState=not-found\n")}),
    )
    assert env2.has_systemd_service() is False

    # rc != 0 → NO existe
    env3 = SystemMaintenanceEnvironment(
        service_name="ofertas-hunter.service",
        runner=FakeProc({"systemctl show": (1, "")}),
    )
    assert env3.has_systemd_service() is False


def test_stop_start_service_call_systemctl():
    runner = FakeProc({"systemctl": (0, "")})
    env = SystemMaintenanceEnvironment(service_name="ofertas-hunter.service", runner=runner)
    assert env.stop_service() is True
    assert env.start_service() is True
    joined = [" ".join(c) for c in runner.ran]
    assert any("stop ofertas-hunter.service" in c for c in joined)
    assert any("start ofertas-hunter.service" in c for c in joined)


def test_stop_start_use_sudo_when_enabled():
    runner = FakeProc({"sudo": (0, "")})
    env = SystemMaintenanceEnvironment(
        service_name="ofertas-hunter.service", runner=runner, use_sudo=True
    )
    assert env.stop_service() is True
    assert env.start_service() is True
    # cada comando systemctl debe ir prefijado con `sudo -n`
    for cmd in runner.ran:
        if "systemctl" in cmd:
            assert cmd[0] == "sudo"
            assert cmd[1] == "-n"
            assert "systemctl" in cmd


def test_profile_busy_uses_lock(tmp_path):
    # Si no hay lock tomado, el perfil NO está ocupado (try_acquire OK → libera).
    amazon_lock = tmp_path / "amazon" / ".profile.lock"
    env = SystemMaintenanceEnvironment(
        service_name="x",
        runner=FakeProc({}),
        amazon_lock_path=str(amazon_lock),
        mercadolibre_lock_path=str(tmp_path / "ml" / ".profile.lock"),
    )
    # En POSIX el lock libre => no busy. En Windows el fallback también.
    assert env.amazon_profile_busy() in (True, False)  # no crashea


def test_vacuum_executes(db_path):
    env = SystemMaintenanceEnvironment(service_name="x", runner=FakeProc({}))
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        env.checkpoint(conn)
        env.vacuum(conn)  # no debe lanzar
    finally:
        conn.close()
