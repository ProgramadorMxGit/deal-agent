#!/usr/bin/env bash
# scripts/run_worker.sh — corre el worker (orchestrator multi-agente).
# Por ahora delega al hunter Amazon en --once; el orchestrator full vendrá
# en Fase 5.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"
PYTHON="${PYTHON:-.venv/bin/python}"
[ ! -x "$PYTHON" ] && PYTHON=".venv-linux/bin/python"
[ ! -x "$PYTHON" ] && PYTHON="python3"
exec "${PYTHON}" -m ofertas_hunter amazon-hunt "$@"
