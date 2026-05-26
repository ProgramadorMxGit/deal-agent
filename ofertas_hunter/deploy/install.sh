#!/usr/bin/env bash
# deploy/install.sh — instala ofertas_hunter en un VPS Ubuntu.
#
# Uso (como root o con sudo):
#   sudo PROJECT_ROOT=/opt/ofertas-hunter SERVICE_USER=ofertas \
#        deploy/install.sh
#
# Crea el usuario, copia las units de systemd renderizadas con los paths
# correctos y las habilita. NO arranca los servicios automáticamente: el
# operador decide cuando activar publicación real.

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/opt/ofertas-hunter}"
SERVICE_USER="${SERVICE_USER:-ofertas}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ "$(id -u)" != "0" ]]; then
    echo "ERROR: ejecutar como root (sudo)." >&2
    exit 1
fi

echo "[install] PROJECT_ROOT=${PROJECT_ROOT}"
echo "[install] SERVICE_USER=${SERVICE_USER}"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${SERVICE_USER}"
    echo "[install] usuario '${SERVICE_USER}' creado"
fi

mkdir -p "${PROJECT_ROOT}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${PROJECT_ROOT}"

# Render unit files
for unit in ofertas-hunter.service \
            ofertas-hunter-dispatcher.service \
            ofertas-hunter-telegram.service \
            ofertas-hunter-maintenance.service \
            ofertas-hunter-maintenance.timer; do
    src="${PROJECT_ROOT}/deploy/systemd/${unit}"
    if [[ ! -f "${src}" ]]; then
        echo "[install] skip ${unit} (no existe en ${src})"
        continue
    fi
    sed -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
        -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
        "${src}" > "${SYSTEMD_DIR}/${unit}"
    chmod 0644 "${SYSTEMD_DIR}/${unit}"
    echo "[install] ${SYSTEMD_DIR}/${unit}"
done

systemctl daemon-reload

cat <<EOF
[install] OK.

Próximos pasos (sin envío real):
  1. Como ${SERVICE_USER}, crear venv y instalar deps:
       sudo -iu ${SERVICE_USER}
       cd ${PROJECT_ROOT}
       python3.11 -m venv .venv
       source .venv/bin/activate
       pip install -r requirements.txt
       playwright install chromium
       cp .env.example .env

  2. Inicializar la DB:
       python -m ofertas_hunter init-db
       python -m ofertas_hunter check-db

  3. Habilitar servicios (NO los arranca todavía):
       sudo systemctl enable ofertas-hunter
       sudo systemctl enable ofertas-hunter-dispatcher
       sudo systemctl enable --now ofertas-hunter-maintenance.timer

  4. Validar config:
       python -m ofertas_hunter check-config
       python -m ofertas_hunter status

  5. Cuando estés listo (PUBLISHING_DRY_RUN=true por default):
       sudo systemctl start ofertas-hunter
       sudo systemctl start ofertas-hunter-dispatcher

  6. Para envío real (sólo cuando lo confirmes manualmente):
       editar .env: PUBLISHING_ENABLED=true PUBLISHING_DRY_RUN=false
       sudo systemctl restart ofertas-hunter-dispatcher
EOF
