"""Tests para el subcomando `mcp-serve` del CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ofertas_hunter.__main__ import main, cmd_mcp_serve
from ofertas_hunter.mcp.lockfile import FileLock


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path: Path, monkeypatch) -> None:
    """Cada test usa su propia DB para no pisar producción."""
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    monkeypatch.setenv("DB_PATH", str(db_dir / "test.db"))


def test_mcp_serve_subparser_is_registered() -> None:
    """`mcp-serve` aparece como subcomando válido en el parser."""
    import argparse

    from ofertas_hunter.__main__ import main as cli_main

    # main() con un comando inexistente sale con SystemExit 2.
    # main() con `mcp-serve --help` sale con SystemExit 0 desde argparse.
    with pytest.raises(SystemExit) as exc:
        cli_main(["mcp-serve", "--help"])
    assert exc.value.code == 0


def test_cmd_mcp_serve_exits_2_when_lock_held_by_run(tmp_path, monkeypatch, capsys) -> None:
    """Si `data/mcp_serve.lock` ya existe con un pid vivo, salimos con código 2."""
    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    lock_path = db_dir / "mcp_serve.lock"
    # Simular un holder vivo (nuestro propio pid)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "scope": "run", "created_at": "x"})
    )

    monkeypatch.setenv("DB_PATH", str(db_dir / "test.db"))
    import argparse

    args = argparse.Namespace(no_lock=False, log_level="ERROR")
    rc = cmd_mcp_serve(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert "ya hay otra instancia activa" in captured.err


def test_cmd_mcp_serve_with_no_lock_skips_acquire(tmp_path, monkeypatch) -> None:
    """Con `--no-lock` no se adquiere lockfile.

    Como serve_stdio bloquea, monkeypatcheamos la clase para no entrar al loop.
    """
    db_dir = tmp_path / "data"
    db_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DB_PATH", str(db_dir / "test.db"))

    # Reemplazar serve_stdio para retornar sin hacer nada
    from ofertas_hunter.mcp import server as mcp_server_module

    async def _fake_serve(self):
        return None

    monkeypatch.setattr(mcp_server_module.MCPServer, "serve_stdio", _fake_serve)

    import argparse

    args = argparse.Namespace(no_lock=True, log_level="ERROR")
    rc = cmd_mcp_serve(args)
    assert rc == 0
    # Lockfile no debe existir
    assert not (db_dir / "mcp_serve.lock").exists()
