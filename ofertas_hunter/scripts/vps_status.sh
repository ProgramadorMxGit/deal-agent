#!/usr/bin/env bash
sudo systemctl status ofertas-hunter --no-pager
echo
cd /opt/deal-agent/ofertas_hunter
source .venv/bin/activate
python -m ofertas_hunter status
