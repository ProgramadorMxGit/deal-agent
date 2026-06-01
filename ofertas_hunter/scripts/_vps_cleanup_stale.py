#!/usr/bin/env python3
"""Wrapper de paridad para `_vps_cleanup_stale.py`.

Este script delega en el subcomando consolidado:

    python -m ofertas_hunter saneamiento --task outbox-stale [--apply] [--marketplace ...]

Se conserva como wrapper de una línea para no romper invocaciones
externas (cron, runbooks, scripts ad-hoc en el VPS) durante la
migración. Los flags `--apply` y `--marketplace VALOR` se reenvían
sin modificación; el exit code del subproceso se propaga tal cual.

Reemplaza la implementación inline del script original (paridad
SQL en `src/ofertas_hunter/saneamiento/tasks/outbox_stale.py`).
"""
from __future__ import annotations

import subprocess
import sys


_TASK_NAME = "outbox-stale"


def _forward_args(argv: list[str]) -> list[str]:
    """Reenvía solo los flags relevantes (--apply, --marketplace).

    Cualquier otro flag se ignora silenciosamente para preservar
    compatibilidad con invocaciones legacy que pudieran pasar args
    obsoletos.
    """
    forwarded: list[str] = []
    i = 0
    args = argv[1:]
    while i < len(args):
        a = args[i]
        if a == "--apply":
            forwarded.append("--apply")
            i += 1
        elif a == "--marketplace":
            if i + 1 < len(args):
                forwarded.extend(["--marketplace", args[i + 1]])
                i += 2
            else:
                i += 1
        elif a.startswith("--marketplace="):
            forwarded.append(a)
            i += 1
        else:
            i += 1
    return forwarded


def main() -> int:
    cmd = [
        sys.executable, "-m", "ofertas_hunter", "saneamiento",
        "--task", _TASK_NAME,
    ] + _forward_args(sys.argv)
    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
