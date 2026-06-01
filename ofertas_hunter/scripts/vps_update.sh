#!/usr/bin/env bash
set -euo pipefail
cd /opt/deal-agent
git pull --ff-only
cd /opt/deal-agent/ofertas_hunter
source .venv/bin/activate
pip install -e . --quiet
python -m pytest -q --tb=line || { echo "TESTS FAILED"; exit 1; }
python -m ofertas_hunter check-config | head -15
sudo systemctl restart ofertas-hunter
sleep 3
sudo systemctl status ofertas-hunter --no-pager | head -10
