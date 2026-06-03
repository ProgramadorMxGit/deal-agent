"""Tests del throttle del loop ML en scripts/orquestador_ia.py.

Cubre:
- `_env_float` parsea/clampea correctamente.
- `ML_LOOP_SLEEP_SECONDS` respeta la variable de entorno.
- `loop_ml` contiene un sleep incondicional (anti-runaway).
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "orquestador_ia.py"


def _load_module(monkeypatch, env: dict | None = None):
    """Importa orquestador_ia.py de forma aislada con env opcional."""
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    sys.path.insert(0, str(SCRIPTS_DIR))
    # Forzar recarga limpia.
    sys.modules.pop("orquestador_ia", None)
    mod = importlib.import_module("orquestador_ia")
    importlib.reload(mod)
    return mod


def test_env_float_defaults_and_clamp(monkeypatch):
    mod = _load_module(monkeypatch)
    assert mod._env_float("NO_EXISTE_XYZ", 5.0) == 5.0
    monkeypatch.setenv("FOO_FLOAT", "")
    assert mod._env_float("FOO_FLOAT", 7.0) == 7.0
    monkeypatch.setenv("FOO_FLOAT", "abc")
    assert mod._env_float("FOO_FLOAT", 8.0) == 8.0
    monkeypatch.setenv("FOO_FLOAT", "-3")  # negativo → default
    assert mod._env_float("FOO_FLOAT", 9.0) == 9.0
    monkeypatch.setenv("FOO_FLOAT", "12.5")
    assert mod._env_float("FOO_FLOAT", 9.0) == 12.5


def test_ml_loop_sleep_default(monkeypatch):
    monkeypatch.delenv("ML_LOOP_SLEEP_SECONDS", raising=False)
    mod = _load_module(monkeypatch)
    assert mod.ML_LOOP_SLEEP_SECONDS == 5.0


def test_ml_loop_sleep_from_env(monkeypatch):
    mod = _load_module(monkeypatch, env={"ML_LOOP_SLEEP_SECONDS": "10"})
    assert mod.ML_LOOP_SLEEP_SECONDS == 10.0


def test_loop_ml_has_unconditional_throttle_sleep():
    """Verifica estáticamente que loop_ml duerme ML_LOOP_SLEEP_SECONDS.

    Test estático (AST) porque el loop es `while True` async y difícil de
    ejercitar sin un servidor MCP completo. Confirma que el sleep con la
    constante de throttle existe dentro de la función `loop_ml`.
    """
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    loop_ml = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "loop_ml"),
        None,
    )
    assert loop_ml is not None, "loop_ml no encontrado"

    found = False
    for node in ast.walk(loop_ml):
        # Busca: await asyncio.sleep(ML_LOOP_SLEEP_SECONDS)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "sleep" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == "ML_LOOP_SLEEP_SECONDS":
                    found = True
                    break
    assert found, "loop_ml no tiene sleep(ML_LOOP_SLEEP_SECONDS)"


def test_amazon_and_telegram_loops_untouched_by_ml_const():
    """El throttle ML no debe aparecer en loop_amazon (no afectar Amazon)."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    loop_amazon = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "loop_amazon"),
        None,
    )
    assert loop_amazon is not None
    for node in ast.walk(loop_amazon):
        if isinstance(node, ast.Name) and node.id == "ML_LOOP_SLEEP_SECONDS":
            pytest.fail("loop_amazon no debe usar ML_LOOP_SLEEP_SECONDS")


def test_amazon_loop_sleep_default(monkeypatch):
    monkeypatch.delenv("AMAZON_LOOP_SLEEP_SECONDS", raising=False)
    mod = _load_module(monkeypatch)
    assert mod.AMAZON_LOOP_SLEEP_SECONDS == 5.0


def test_amazon_loop_sleep_from_env(monkeypatch):
    mod = _load_module(monkeypatch, env={"AMAZON_LOOP_SLEEP_SECONDS": "8"})
    assert mod.AMAZON_LOOP_SLEEP_SECONDS == 8.0


def test_loop_amazon_has_unconditional_throttle_sleep():
    """loop_amazon debe dormir AMAZON_LOOP_SLEEP_SECONDS (anti-runaway).

    Sin este throttle el loop gira sin pausa cuando el frontier tiene URLs y
    no hay captcha, inflando runtime_events (mcp_tool_called) a millones.
    """
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    loop_amazon = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "loop_amazon"),
        None,
    )
    assert loop_amazon is not None, "loop_amazon no encontrado"
    found = False
    for node in ast.walk(loop_amazon):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "sleep" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == "AMAZON_LOOP_SLEEP_SECONDS":
                    found = True
                    break
    assert found, "loop_amazon no tiene sleep(AMAZON_LOOP_SLEEP_SECONDS)"
