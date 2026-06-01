"""Tests unitarios para ``ofertas_hunter.saneamiento.lockfile.SaneamientoLock``.

Estos tests definen el contrato de la API descrito en el design.md del spec
``bot-saneamiento-vps`` (sección ``Components and Interfaces`` →
``SaneamientoLock``). Se escriben antes de la implementación (TDD-RED): la
tarea 2.2 hará que estos tests pasen.

Contrato resumido:

- ``SaneamientoLock(path, mcp_lock_path, *, pid_alive=...)`` — el callable
  ``pid_alive`` se inyecta para que los tests puedan simular pid vivos /
  muertos de forma determinista; en producción su default consulta la
  realidad del SO (paridad con ``ofertas_hunter.mcp.lockfile``).
- ``acquire()`` escribe el lock y devuelve ``LockAcquisition``.
- ``acquire()`` levanta ``SaneamientoLockBusy`` si el lock está vivo de otro
  proceso, sin tocar el archivo.
- ``acquire()`` sobrescribe locks huérfanos (pid muerto).
- ``acquire()`` consulta ``mcp_serve.lock`` sólo para reportar concurrencia
  (``mcp_serve_active``), nunca lo modifica.
- ``release()`` borra el lock y es idempotente.

Los archivos de lock son JSON UTF-8 con la forma::

    {"pid": <int>, "scope": "saneamiento", "started_at": "<ISO-Z>"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from ofertas_hunter.saneamiento.lockfile import (
    LockAcquisition,
    SaneamientoLock,
    SaneamientoLockBusy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FOREIGN_PID = 424242  # cualquier pid distinto del proceso de tests
_FOREIGN_STARTED_AT = "2026-05-29T11:00:00.000Z"
_MCP_PID = 999111
_MCP_STARTED_AT = "2026-05-29T10:30:00.000Z"


def _write_lock(path: Path, *, pid: int, scope: str, started_at: str) -> None:
    """Escribe un lock con el formato canónico del design."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "scope": scope, "started_at": started_at}),
        encoding="utf-8",
    )


def _read_lock(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _alive_always(_pid: int) -> bool:
    return True


def _alive_never(_pid: int) -> bool:
    return False


# ---------------------------------------------------------------------------
# acquire() — caso path libre (Requirement 2.6)
# ---------------------------------------------------------------------------


def test_acquire_writes_lock_when_path_is_free(tmp_path: Path) -> None:
    """Si no hay lock previo, ``acquire()`` lo escribe con pid+scope+started_at."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_never,
    )

    result = lock.acquire()

    assert isinstance(result, LockAcquisition)
    assert result.mcp_serve_active is False
    assert lock_path.exists(), "acquire() debe escribir el lock en disco"

    payload = _read_lock(lock_path)
    assert payload["pid"] == os.getpid()
    assert payload["scope"] == "saneamiento"
    assert isinstance(payload["started_at"], str)
    assert payload["started_at"].endswith("Z"), (
        "started_at debe usar sufijo 'Z' (UTC) según el design"
    )

    # El holder devuelto refleja exactamente lo escrito en disco.
    assert result.holder["pid"] == os.getpid()
    assert result.holder["started_at"] == payload["started_at"]


# ---------------------------------------------------------------------------
# acquire() — caso lock vivo de otro pid (Requirement 2.7)
# ---------------------------------------------------------------------------


def test_acquire_raises_busy_when_lock_held_by_alive_process(
    tmp_path: Path,
) -> None:
    """Si el lock existe y su pid sigue vivo, ``acquire()`` aborta sin tocarlo."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    _write_lock(
        lock_path,
        pid=_FOREIGN_PID,
        scope="saneamiento",
        started_at=_FOREIGN_STARTED_AT,
    )
    original_bytes = lock_path.read_bytes()

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_always,
    )

    with pytest.raises(SaneamientoLockBusy) as excinfo:
        lock.acquire()

    holder = excinfo.value.holder
    assert holder["pid"] == _FOREIGN_PID
    assert holder["started_at"] == _FOREIGN_STARTED_AT

    # El archivo en disco NO se altera cuando otro proceso lo tiene.
    assert lock_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# acquire() — caso lock huérfano (pid muerto)
# ---------------------------------------------------------------------------


def test_acquire_overwrites_orphan_lock_when_holder_is_dead(
    tmp_path: Path,
) -> None:
    """Si el lock existe pero su pid está muerto, ``acquire()`` lo sobrescribe."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    _write_lock(
        lock_path,
        pid=_FOREIGN_PID,
        scope="saneamiento",
        started_at=_FOREIGN_STARTED_AT,
    )

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_never,
    )

    result = lock.acquire()

    assert isinstance(result, LockAcquisition)
    assert result.mcp_serve_active is False

    payload = _read_lock(lock_path)
    assert payload["pid"] == os.getpid(), (
        "El lock huérfano debe ser sobreescrito con el pid actual"
    )
    assert payload["scope"] == "saneamiento"
    assert payload["started_at"] != _FOREIGN_STARTED_AT, (
        "started_at debe refrescarse al reescribir el lock"
    )
    assert payload["started_at"].endswith("Z")


# ---------------------------------------------------------------------------
# acquire() — convivencia con mcp_serve.lock (Requirement 2.5)
# ---------------------------------------------------------------------------


def test_acquire_with_alive_mcp_serve_lock_does_not_abort(
    tmp_path: Path,
) -> None:
    """Si ``mcp_serve.lock`` está vivo, la run continúa y reporta concurrencia."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    _write_lock(
        mcp_path,
        pid=_MCP_PID,
        scope="mcp-serve",
        started_at=_MCP_STARTED_AT,
    )
    original_mcp_bytes = mcp_path.read_bytes()

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_always,
    )

    result = lock.acquire()

    # No aborta: la saneamiento.lock se escribe y mcp_serve_active queda True.
    assert isinstance(result, LockAcquisition)
    assert result.mcp_serve_active is True
    assert lock_path.exists()

    # mcp_serve.lock NO debe ser tocada bajo ninguna circunstancia.
    assert mcp_path.read_bytes() == original_mcp_bytes


def test_acquire_with_orphan_mcp_serve_lock_reports_inactive_and_does_not_touch_it(
    tmp_path: Path,
) -> None:
    """Si ``mcp_serve.lock`` está huérfano, ``mcp_serve_active`` es False y el archivo no se toca."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    _write_lock(
        mcp_path,
        pid=_MCP_PID,
        scope="mcp-serve",
        started_at=_MCP_STARTED_AT,
    )
    original_mcp_bytes = mcp_path.read_bytes()

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_never,
    )

    result = lock.acquire()

    assert isinstance(result, LockAcquisition)
    assert result.mcp_serve_active is False

    # El archivo de mcp_serve.lock se preserva intacto: el bot de saneamiento
    # nunca lo modifica (solo lo lee).
    assert mcp_path.exists()
    assert mcp_path.read_bytes() == original_mcp_bytes


# ---------------------------------------------------------------------------
# release()
# ---------------------------------------------------------------------------


def test_release_deletes_lock_file(tmp_path: Path) -> None:
    """``release()`` borra el archivo de lock."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_never,
    )
    lock.acquire()
    assert lock_path.exists()

    lock.release()

    assert not lock_path.exists(), "release() debe borrar el lock del disco"


def test_release_is_idempotent_when_file_already_gone(tmp_path: Path) -> None:
    """Llamar ``release()`` dos veces seguidas no debe levantar excepción."""

    lock_path = tmp_path / "saneamiento.lock"
    mcp_path = tmp_path / "mcp_serve.lock"

    lock = SaneamientoLock(
        path=lock_path,
        mcp_lock_path=mcp_path,
        pid_alive=_alive_never,
    )
    lock.acquire()

    lock.release()
    # Segundo release() — sin archivo en disco — no debe romper.
    lock.release()

    assert not lock_path.exists()
