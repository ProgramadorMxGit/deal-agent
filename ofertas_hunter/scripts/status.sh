#!/usr/bin/env bash
# scripts/status.sh — VPS / Linux
# Estado rápido del bot (DB + agentes + outbox + runtime events).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"
PYTHON="${PYTHON:-.venv/bin/python}"
[ ! -x "$PYTHON" ] && PYTHON=".venv-linux/bin/python"
[ ! -x "$PYTHON" ] && PYTHON="python3"
exec "${PYTHON}" -m ofertas_hunter status "$@"
