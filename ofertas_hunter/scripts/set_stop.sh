#!/bin/bash
cd /opt/deal-agent/ofertas_hunter || exit 1
# 24h + 1h margen = 90000s
echo $(( $(date +%s) + 90000 )) > logs/.diversity_snapshot_stop_epoch
echo "stop_epoch set to: $(cat logs/.diversity_snapshot_stop_epoch)"
echo "now epoch:         $(date +%s)"
echo "stop in (h):       $(( ( $(cat logs/.diversity_snapshot_stop_epoch) - $(date +%s) ) / 3600 ))"
