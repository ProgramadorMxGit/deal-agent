"""Tests del ProfileLock (lock de archivo cross-process para perfiles Chromium)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ofertas_hunter.runtime.profile_lock import ProfileLock


def test_acquire_and_release(tmp_path: Path):
    lock_path = tmp_path / "amazon.lock"
    lock = ProfileLock(str(lock_path))
    assert lock.try_acquire() is True
    assert lock.held is True
    lock.release()
    assert lock.held is False


def test_release_is_idempotent(tmp_path: Path):
    lock = ProfileLock(str(tmp_path / "x.lock"))
    lock.try_acquire()
    lock.release()
    # Segundo release no debe romper.
    lock.release()
    assert lock.held is False


def test_context_manager_releases(tmp_path: Path):
    lock_path = str(tmp_path / "cm.lock")
    with ProfileLock(lock_path) as acquired:
        assert acquired is True
    # Tras salir, otro lock sobre el mismo archivo debe poder tomarse.
    other = ProfileLock(lock_path)
    assert other.try_acquire() is True
    other.release()


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl real solo en POSIX")
def test_second_lock_busy_while_first_held(tmp_path: Path):
    """En POSIX, un segundo proceso/handle NO debe poder tomar el lock
    mientras el primero lo tiene. Simulamos con dos instancias que abren
    descriptores distintos sobre el mismo archivo."""
    lock_path = str(tmp_path / "busy.lock")
    first = ProfileLock(lock_path)
    assert first.try_acquire() is True
    second = ProfileLock(lock_path)
    # No bloqueante: debe reportar que está ocupado.
    assert second.try_acquire() is False
    assert second.held is False
    first.release()
    # Ahora sí.
    assert second.try_acquire() is True
    second.release()
