#!/usr/bin/env bash
# Diagnóstico Amazon: cuántos captchas reales, en qué patrón, cuándo
# empezaron, cuántos snapshots, qué profile cookies hay. NO toca el bot.

set -uo pipefail
DB=/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db
DEBUG_DIR=/opt/deal-agent/ofertas_hunter/data/debug/amazon_captcha
PROFILE_DIR=/opt/deal-agent/ofertas_hunter/secrets/browser_profiles/amazon

echo "===== 1) PROCESOS ACTIVOS ====="
ps -eo pid,etime,user,cmd 2>/dev/null | grep -E 'orquestador_ia|ofertas_hunter|kiro-cli|playwright' | grep -v grep | head -10
echo

echo "===== 2) UPTIME DEL ORQUESTADOR (según logs DB) ====="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 30 30
SELECT 'first_event_today' AS k, MIN(created_at) AS v FROM runtime_events
  WHERE created_at >= datetime('now','-24 hours')
UNION ALL
SELECT 'last_event' AS k, MAX(created_at) AS v FROM runtime_events
UNION ALL
SELECT 'mcp_tool_calls_last_30min' AS k, COUNT(*)||'' AS v FROM runtime_events
  WHERE kind='mcp_tool_called' AND created_at >= datetime('now','-30 minutes');
SQL
echo

echo "===== 3) AMAZON CAPTCHAS (últimos 60 min) ====="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
.width 19 30 5
SELECT substr(created_at,1,19) AS ts, kind,
       SUM(1) AS n
FROM runtime_events
WHERE created_at >= datetime('now','-60 minutes')
  AND (kind LIKE '%captcha%' OR kind LIKE '%503%' OR kind LIKE '%backoff%' OR kind LIKE '%pause%')
GROUP BY substr(created_at,1,16), kind
ORDER BY ts DESC LIMIT 20;
SQL
echo

echo "===== 4) DISTRIBUCION TIMESTAMPS DE SNAPSHOTS RECIENTES ====="
ls -1tr "$DEBUG_DIR" 2>/dev/null | tail -15 | awk -F_ '{print $1"_"$2}' | sort -u | tail -10
echo "Total snapshots:"
ls "$DEBUG_DIR" 2>/dev/null | wc -l
echo

echo "===== 5) PROFILE AMAZON (tamaño + cookies recientes) ====="
echo "Files: $(find $PROFILE_DIR -type f 2>/dev/null | wc -l)"
echo "Size:  $(du -sh $PROFILE_DIR 2>/dev/null | cut -f1)"
echo "Cookies file: $PROFILE_DIR/Default/Cookies"
ls -la "$PROFILE_DIR/Default/Cookies" 2>/dev/null
echo

echo "===== 6) URLS DE PRODUCTO PROCESADAS RECIENTEMENTE (frontier) ====="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
SELECT status, COUNT(*) AS n
FROM frontier_urls
WHERE marketplace='amazon' AND kind='product'
GROUP BY status
ORDER BY n DESC;
SQL
echo

echo "===== 7) ULTIMOS 10 EVENTOS WARNING+ ====="
sqlite3 -readonly "$DB" <<'SQL'
.mode line
SELECT substr(created_at,1,19) AS ts, severity, kind,
       substr(coalesce(payload_json,''),1,250) AS payload
FROM runtime_events
WHERE severity IN ('warning','error','critical')
ORDER BY id DESC LIMIT 10;
SQL
echo

echo "===== 8) RATE DE PUBLICACIONES + OUTBOX SEND ====="
sqlite3 -readonly "$DB" <<'SQL'
.mode column
.headers on
SELECT 'sent_total' AS k, COUNT(*)||'' AS v FROM outbox WHERE state='sent'
UNION ALL
SELECT 'sent_last_30min' AS k, COUNT(*)||'' AS v FROM outbox
  WHERE state='sent' AND last_attempt_at >= datetime('now','-30 minutes')
UNION ALL
SELECT 'pending_total' AS k, COUNT(*)||'' AS v FROM outbox WHERE state='pending'
UNION ALL
SELECT 'failed_total' AS k, COUNT(*)||'' AS v FROM outbox WHERE state='failed';
SQL
