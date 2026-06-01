#!/bin/bash
# Diagnóstico rápido sin LIKE pesados.
cd /opt/deal-agent/ofertas_hunter
echo "=== DB size ==="
ls -lh data/ofertas_hunter.db | awk '{print $5}'
echo
echo "=== runtime_events: rowid range (no full scan) ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db "SELECT MAX(id) - MIN(id) AS approx_rows, MAX(id) AS max_id, MIN(id) AS min_id FROM runtime_events"
echo
echo "=== runtime_events: ultimas 3 entradas (por id DESC, sin scan completo) ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db "SELECT id, created_at, kind, severity FROM runtime_events ORDER BY id DESC LIMIT 3"
echo
echo "=== published_messages: ultimas 5 ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db "SELECT id, sent_at, success FROM published_messages ORDER BY id DESC LIMIT 5"
echo
echo "=== outbox: estado ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db "SELECT state, COUNT(*) FROM outbox GROUP BY state"
echo
echo "=== Tablas con mas filas (estimadas via dbstat) ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db "SELECT name, SUM(pgsize) / 1024 / 1024 AS mb FROM dbstat GROUP BY name ORDER BY mb DESC LIMIT 10" 2>/dev/null || echo "(dbstat no disponible - lo intento con sqlite_master)"
echo
echo "=== Indices en runtime_events ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db ".indexes runtime_events"
echo
echo "=== Schema runtime_events ==="
sqlite3 -cmd ".timeout 30000" data/ofertas_hunter.db ".schema runtime_events"
