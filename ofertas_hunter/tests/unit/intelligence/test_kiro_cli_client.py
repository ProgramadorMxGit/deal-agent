"""Tests del KiroCliClient (wrapper async sobre subprocess kiro-cli)."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ofertas_hunter.intelligence.kiro_cli_client import (
    KiroCliClient,
    KiroCliConfig,
    resolve_kiro_cli_path,
)


# ---------------------------------------------------------------------------
# resolve_kiro_cli_path
# ---------------------------------------------------------------------------


def test_resolve_uses_env_var_when_set(monkeypatch, tmp_path):
    fake = tmp_path / "kiro-cli"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setenv("KIRO_CLI", str(fake))
    assert resolve_kiro_cli_path() == str(fake)


def test_resolve_falls_back_to_string_when_nothing_found(monkeypatch):
    monkeypatch.delenv("KIRO_CLI", raising=False)
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.exists", return_value=False):
        result = resolve_kiro_cli_path()
        assert result == "kiro-cli"


# ---------------------------------------------------------------------------
# KiroCliClient.ask_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_json_parses_pure_json_stdout():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(b'{"chosen_id": 42, "reason": "ok"}', b"")
    )

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result == {"chosen_id": 42, "reason": "ok"}


@pytest.mark.asyncio
async def test_ask_json_extracts_json_block_from_noisy_stdout():
    """Algunas versiones de kiro-cli devuelven texto + JSON; debe extraer."""
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            b'Thinking...\nResult:\n{"chosen_id": 7, "reason": "ok"}\n\nDone.',
            b"",
        )
    )
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result == {"chosen_id": 7, "reason": "ok"}


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_timeout():
    client = KiroCliClient(
        KiroCliConfig(binary_path="/fake/kiro-cli", timeout_seconds=0.1)
    )

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
        result = await client.ask_json("test")
    assert result is None
    fake_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_nonzero_exit():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"some error"))

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_returns_none_on_unparseable_stdout():
    client = KiroCliClient(KiroCliConfig(binary_path="/fake/kiro-cli"))

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"hola sin json", b""))

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_returns_none_when_binary_not_found():
    client = KiroCliClient(KiroCliConfig(binary_path="/no/existe/kiro-cli"))

    with patch(
        "asyncio.create_subprocess_exec", side_effect=FileNotFoundError()
    ):
        result = await client.ask_json("test")
    assert result is None


@pytest.mark.asyncio
async def test_ask_json_propagates_cancellation():
    """Si el caller cancela la tarea, el subprocess debe morir y la
    cancelación debe propagarse (no swallow-earse).

    Importante: NO mockeamos ``asyncio.wait_for`` — dejamos que el real
    vea el ``CancelledError`` lanzado desde ``communicate``. Así
    ejercitamos el flujo completo del except clause.
    """
    client = KiroCliClient(
        KiroCliConfig(binary_path="/fake/kiro-cli", timeout_seconds=5.0)
    )

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        with pytest.raises(asyncio.CancelledError):
            await client.ask_json("test")

    # Subprocess debe haber sido terminado: kill llamado y wait awaited.
    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_json_kill_failure_logged_not_swallowed_silently(caplog):
    """Si ``proc.kill()`` falla durante el cleanup de timeout, el error
    debe loguearse (no ``except Exception: pass``), pero el flujo debe
    continuar y devolver ``None`` sin re-lanzar.
    """
    client = KiroCliClient(
        KiroCliConfig(binary_path="/fake/kiro-cli", timeout_seconds=0.1)
    )

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    fake_proc.kill = MagicMock(side_effect=OSError("kill failed"))
    fake_proc.wait = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="ofertas_hunter.intelligence.kiro_cli_client"):
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await client.ask_json("test")

    # Flujo gracioso: ``None`` y sin excepción.
    assert result is None
    fake_proc.kill.assert_called_once()
    # Algo se logueó (al menos el timeout warning, idealmente también el
    # error de kill via ``logger.exception``).
    assert caplog.records, "se esperaban logs warnings/errors durante el cleanup"
    # Verificar que el error de kill quedó registrado (no silencioso).
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "kill" in messages.lower() or "matando" in messages.lower()
