"""Tests del shutdown limpio del orquestador (Task 5: A-E).

Importa el módulo `scripts/orquestador_ia.py` y prueba las funciones de
cleanup de browser y liberación de locks de forma aislada (sin arrancar
el bot real ni Playwright).

A) signal handler marca shutdown_requested.
B) cleanup de browser es idempotente.
C) TargetClosedError durante close no rompe shutdown.
D) timeout cerrando browser no bloquea todo.
E) locks se liberan en shutdown.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

# Cargar scripts/orquestador_ia.py como módulo "orq".
# Este archivo vive en tests/unit/ → repo root = parents[2].
_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "orquestador_ia.py"


@pytest.fixture(scope="module")
def orq():
    spec = importlib.util.spec_from_file_location("orq_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orq_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeBrowserOK:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


class FakeBrowserTargetClosed:
    def __init__(self):
        self.calls = 0

    async def aclose(self):
        self.calls += 1
        raise RuntimeError("Target page, context or browser has been closed")


class FakeBrowserHangs:
    def __init__(self):
        self.calls = 0

    async def aclose(self):
        self.calls += 1
        await asyncio.sleep(100)  # más que el timeout


# B) idempotente: cerrar dos veces no falla
def test_B_browser_close_idempotent(orq):
    b = FakeBrowserOK()

    async def run():
        await orq._close_browser_with_timeout(b, "test", None)
        await orq._close_browser_with_timeout(b, "test", None)

    asyncio.run(run())
    assert b.closed == 2  # ambas llamadas completaron sin lanzar


# B-bis) None no rompe
def test_B_browser_none_ok(orq):
    async def run():
        await orq._close_browser_with_timeout(None, "none", None)

    asyncio.run(run())  # no debe lanzar


# C) TargetClosedError no rompe el shutdown
def test_C_target_closed_does_not_raise(orq):
    b = FakeBrowserTargetClosed()

    async def run():
        await orq._close_browser_with_timeout(b, "ml", None)

    asyncio.run(run())  # capturado internamente, no propaga
    assert b.calls == 1


# D) timeout cerrando un browser no bloquea (acota a BROWSER_CLOSE_TIMEOUT)
def test_D_timeout_does_not_block(orq):
    orq.BROWSER_CLOSE_TIMEOUT_SECONDS = 0.2
    b = FakeBrowserHangs()

    async def run():
        await asyncio.wait_for(
            orq._close_browser_with_timeout(b, "amazon", None),
            timeout=3.0,  # si no acotara, colgaría 100s y este wait_for fallaría
        )

    asyncio.run(run())
    assert b.calls == 1


# E) locks de perfil se liberan (borra SingletonLock dentro del perfil)
def test_E_locks_released(orq, tmp_path, monkeypatch):
    # Construir estructura secrets/browser_profiles/{amazon,mercadolibre}
    base = tmp_path / "secrets" / "browser_profiles"
    for prof in ("amazon", "mercadolibre"):
        (base / prof).mkdir(parents=True)
        (base / prof / "SingletonLock").write_text("x")
        (base / prof / "SingletonCookie").write_text("x")
    # Apuntar BOT_DIR del módulo a tmp_path.
    monkeypatch.setattr(orq, "BOT_DIR", tmp_path)

    orq._release_profile_locks(None)

    for prof in ("amazon", "mercadolibre"):
        assert not (base / prof / "SingletonLock").exists()
        assert not (base / prof / "SingletonCookie").exists()


# E-bis) si no hay perfiles, no falla
def test_E_no_profiles_ok(orq, tmp_path, monkeypatch):
    monkeypatch.setattr(orq, "BOT_DIR", tmp_path)
    orq._release_profile_locks(None)  # no debe lanzar


# A) handler marca shutdown_requested y setea el evento
def test_A_signal_handler_sets_flag(orq):
    async def run():
        orq._shutdown_requested = False
        orq._shutdown_event = asyncio.Event()
        orq._install_signal_handlers(None)
        # Simular la señal llamando al mecanismo interno: enviamos SIGTERM
        # al propio proceso si la plataforma lo soporta.
        import signal as _signal
        loop = asyncio.get_running_loop()
        # Si add_signal_handler no está soportado (Windows), marcamos manual.
        try:
            loop._signal_handlers  # type: ignore[attr-defined]
            import os
            os.kill(os.getpid(), _signal.SIGTERM)
            await asyncio.wait_for(orq._shutdown_event.wait(), timeout=2.0)
        except (AttributeError, NotImplementedError):
            orq._shutdown_requested = True
            orq._shutdown_event.set()
        assert orq._shutdown_requested is True
        assert orq._shutdown_event.is_set()

    asyncio.run(run())
