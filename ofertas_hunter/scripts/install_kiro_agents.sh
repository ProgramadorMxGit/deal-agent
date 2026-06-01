#!/usr/bin/env bash
# install_kiro_agents.sh - wrapper Linux para install_kiro_agents.py
# Crea/actualiza los 5 agentes kiro-cli del bot ofertas_hunter en ~/.kiro/agents/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$BOT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: no se encuentra $PYTHON" >&2
    echo "       Ejecuta primero el bootstrap del venv en el VPS." >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/install_kiro_agents.py"
