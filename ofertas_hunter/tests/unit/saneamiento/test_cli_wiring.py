"""Tests de wiring CLI para el subcomando ``saneamiento``.

Valida dos cosas por subprocess:

1. ``python -m ofertas_hunter saneamiento --help`` expone los flags
   públicos esperados.
2. El path de ``--help`` no importa módulos pesados del Bot_Principal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_PATH = PROJECT_ROOT / "src"

_FORBIDDEN_IMPORTS = (
    "ofertas_hunter.dispatching.dispatcher",
    "ofertas_hunter.dispatching.cooldown",
    "ofertas_hunter.dispatching.outbox",
    "ofertas_hunter.publishing.evolution_client",
    "ofertas_hunter.publishing.whatsapp_publisher",
    "ofertas_hunter.telegram.candidate_builder",
    "ofertas_hunter.telegram.channel_config",
    "ofertas_hunter.telegram.message_parser",
)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(SRC_PATH) if not existing else str(SRC_PATH) + os.pathsep + existing
    )
    return env


def test_saneamiento_help_exposes_expected_flags() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ofertas_hunter", "saneamiento", "--help"],
        cwd=PROJECT_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--task" in result.stdout
    assert "--apply" in result.stdout
    assert "--marketplace" in result.stdout


def test_saneamiento_help_does_not_import_heavy_bot_modules() -> None:
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "ofertas_hunter", "saneamiento", "--help"],
        cwd=PROJECT_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    for module_name in _FORBIDDEN_IMPORTS:
        assert module_name not in result.stderr, module_name
