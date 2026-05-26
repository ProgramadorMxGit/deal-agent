#!/usr/bin/env bash
# scripts/run_dispatcher.sh
#
# Arranca el outbox dispatcher en VPS Ubuntu o entorno *nix.
# Por defecto respeta PUBLISHING_DRY_RUN/PUBLISHING_ENABLED del .env.
#
# Uso:
#   ./scripts/run_dispatcher.sh                  # loop forever
#   ./scripts/run_dispatcher.sh --once           # un solo tick
#
# Para activar envío real (sólo cuando el operador lo autorice):
#   1. Editar .env:
#        PUBLISHING_ENABLED=true
#        PUBLISHING_DRY_RUN=false
#   2. Reiniciar el servicio.

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

echo "[run_dispatcher] cwd=${PROJECT_ROOT}"
echo "[run_dispatcher] python=${PYTHON}"

exec "${PYTHON}" -m ofertas_hunter dispatch "$@"
