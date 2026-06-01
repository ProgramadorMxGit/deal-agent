"""Por qué el orquestador pausa ML — busca warnings recientes."""
import sqlite3, json
from datetime import datetime, timedelta, timezone

conn = sqlite3.connect('data/ofertas_hunter.db')
conn.row_factory = sqlite3.Row

cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

print('=== WARNINGS / ERRORS ML últimas 2h (más recientes primero) ===')
for r in conn.execute(
    "SELECT created_at, kind, severity, payload_json FROM runtime_events "
    "WHERE created_at >= ? AND severity IN ('warning','error','critical') "
    "AND (kind LIKE '%ml%' OR kind LIKE '%mercadolibre%' OR kind LIKE '%cookie%' OR kind LIKE '%session%') "
    "ORDER BY id DESC LIMIT 20",
    (cutoff,),
):
    print(f"{r['created_at']} | {r['kind']:40s} | {r['severity']:8s}")
    print(f"    {r['payload_json'][:300]}")

print()
print('=== TODOS los warnings/errors últimas 2h (top 10) ===')
for r in conn.execute(
    "SELECT created_at, kind, severity FROM runtime_events "
    "WHERE created_at >= ? AND severity IN ('warning','error','critical') "
    "ORDER BY id DESC LIMIT 10",
    (cutoff,),
):
    print(f"{r['created_at']} | {r['kind']:50s} | {r['severity']}")

print()
print('=== ¿Qué encontró get_recent_events últimas 2 invocaciones? ===')
for r in conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' AND payload_json LIKE '%get_recent_events%' "
    "AND payload_json LIKE '%phase\":\"after%' "
    "ORDER BY id DESC LIMIT 2"
):
    print(r['created_at'])
    p = json.loads(r['payload_json'])
    rs = p.get('result_summary', {})
    if 'data' in rs:
        for ev in rs['data'][:5]:
            print(f"   -> {ev.get('kind')} | {ev.get('severity')} | {str(ev.get('payload',{}))[:200]}")


print()
print('=== PAYLOADS de mcp_marketplace_paused (orden DESC) ===')
for r in conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_marketplace_paused' "
    "ORDER BY id DESC LIMIT 6"
):
    print(r['created_at'])
    print(' ', r['payload_json'][:500])
    print()

print('=== ¿Por qué se llamó pause_marketplace para ML? ===')
print('Buscamos los eventos JUSTO ANTES de mcp_marketplace_paused mercadolibre...')
ml_pause = conn.execute(
    "SELECT id, created_at FROM runtime_events "
    "WHERE kind='mcp_marketplace_paused' AND payload_json LIKE '%mercadolibre%' "
    "ORDER BY id DESC LIMIT 1"
).fetchone()
if ml_pause:
    print(f"ML pause event id={ml_pause['id']} at {ml_pause['created_at']}")
    for r in conn.execute(
        "SELECT created_at, kind, severity, payload_json FROM runtime_events "
        "WHERE id < ? AND id > ? - 30 "
        "ORDER BY id DESC",
        (ml_pause['id'], ml_pause['id']),
    ):
        print(f"  {r['created_at']} | {r['kind']:40s} | {r['severity']}")
        if r['kind'] in ('mcp_tool_called',) and 'pause_marketplace' in r['payload_json']:
            print('     ', r['payload_json'][:400])
