"""Tests para `ofertas_hunter.mcp.lockfile.FileLock`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ofertas_hunter.mcp.lockfile import FileLock, LockConflict


def test_acquire_creates_file_with_pid_and_scope(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "mcp.lock", scope="mcp-serve")
    lock.acquire()
    payload = json.loads((tmp_path / "mcp.lock").read_text())
    assert payload["pid"] == os.getpid()
    assert payload["scope"] == "mcp-serve"
    assert "created_at" in payload
    lock.release()


def test_acquire_when_holder_alive_raises_conflict(tmp_path: Path) -> None:
    """Si el lockfile tiene un pid vivo (el nuestro con otro scope), conflict."""
    path = tmp_path / "mcp.lock"
    path.write_text(
        json.dumps({"pid": os.getpid(), "scope": "run", "created_at": "x"})
    )
    lock = FileLock(path, scope="mcp-serve")
    with pytest.raises(LockConflict) as exc:
        lock.acquire()
    assert exc.value.holder["scope"] == "run"
    assert exc.value.holder["pid"] == os.getpid()


def test_acquire_when_holder_dead_overwrites_stale_lock(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    # PID que con altísima probabilidad no existe
    path.write_text(
        json.dumps({"pid": 999_999_999, "scope": "run", "created_at": "x"})
    )
    lock = FileLock(path, scope="mcp-serve")
    lock.acquire()
    payload = json.loads(path.read_text())
    assert payload["pid"] == os.getpid()
    assert payload["scope"] == "mcp-serve"
    lock.release()


def test_release_removes_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    lock = FileLock(path, scope="run")
    lock.acquire()
    assert path.exists()
    lock.release()
    assert not path.exists()


def test_double_release_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    lock = FileLock(path, scope="run")
    lock.acquire()
    lock.release()
    # No debe levantar
    lock.release()


def test_re_acquire_with_same_pid_and_scope_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    lock = FileLock(path, scope="run")
    lock.acquire()
    # Otra instancia del mismo proceso/scope no debe fallar
    other = FileLock(path, scope="run")
    other.acquire()
    other.release()


def test_is_held_returns_holder_when_alive(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    lock = FileLock(path, scope="mcp-serve")
    lock.acquire()
    holder = lock.is_held()
    assert holder is not None
    assert holder["pid"] == os.getpid()
    assert holder["scope"] == "mcp-serve"
    lock.release()


def test_is_held_returns_none_when_dead(tmp_path: Path) -> None:
    path = tmp_path / "mcp.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "scope": "run", "created_at": "x"})
    )
    lock = FileLock(path, scope="mcp-serve")
    assert lock.is_held() is None
