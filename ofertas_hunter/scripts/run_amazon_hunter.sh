#!/usr/bin/env bash
# scripts/run_amazon_hunter.sh — VPS Ubuntu / Linux
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ -d ".venv" ]]; then
    PYTHON=".venv/bin/python"
elif [[ -d ".venv-linux" ]]; then
    PYTHON=".venv-linux/bin/python"
else
    PYTHON="python3"
fi

exec "${PYTHON}" -m ofertas_hunter amazon-hunt "$@"
