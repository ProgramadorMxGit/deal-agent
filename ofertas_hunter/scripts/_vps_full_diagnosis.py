"""Diagnóstico profundo del bot — investiga por qué dejó de publicar."""
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from collections import Counter

conn = sqlite3.connect('data/ofertas_hunter.db')
conn.row_factory = sqlite3.Row

NOW = datetime.now(timezone.utc)


def fmt_age(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = NOW - dt
        if delta.total_seconds() < 60:
            return f"hace {int(delta.total_seconds())}s"
        if delta.total_seconds() < 3600:
            return f"hace {int(delta.total_seconds()/60)}min"
        if delta.total_seconds() < 86400:
            return f"hace {delta.total_seconds()/3600:.1f}h"
        return f"hace {delta.days}d {(delta.seconds//3600)}h"
    except Exception:
        return iso_str


print("=" * 70)
print(f"DIAGNÓSTICO PROFUNDO @ {NOW.isoformat(timespec='seconds')}")
print("=" * 70)

# ========================================================================
# 1. ÚLTIMA publicación exitosa
# ========================================================================
print("\n[1] ÚLTIMA PUBLICACIÓN EXITOSA")
last_pub = conn.execute(
    "SELECT id, sent_at, success, outbox_id, offer_id, message_text "
    "FROM published_messages WHERE success = 1 ORDER BY id DESC LIMIT 1"
).fetchone()
if last_pub:
    print(f"  id={last_pub['id']} sent_at={last_pub['sent_at']} ({fmt_age(last_pub['sent_at'])})")
    print(f"  message: {last_pub['message_text'][:120] if last_pub['message_text'] else 'N/A'}")
else:
    print("  Sin publicaciones exitosas registradas")

# Última publicación TOTAL (incluyendo fallidas)
last_any = conn.execute(
    "SELECT id, sent_at, success FROM published_messages ORDER BY id DESC LIMIT 5"
).fetchall()
print(f"\n  Últimas 5 entradas en published_messages:")
for r in last_any:
    print(f"    id={r['id']} sent_at={r['sent_at']} success={r['success']} ({fmt_age(r['sent_at'])})")

# ========================================================================
# 2. Estado del OUTBOX
# ========================================================================
print("\n[2] ESTADO OUTBOX")
print("  Por estado:")
for r in conn.execute("SELECT state, COUNT(*) n FROM outbox GROUP BY state ORDER BY n DESC"):
    print(f"    {r['state']:12s}: {r['n']}")
print("  Por type:")
for r in conn.execute("SELECT type, COUNT(*) n FROM outbox GROUP BY type ORDER BY n DESC"):
    print(f"    {r['type']:12s}: {r['n']}")

# Items pending recientes
print("\n  Items pending (más recientes primero, top 5):")
for r in conn.execute(
    "SELECT id, type, enqueued_at, attempts, last_attempt_at "
    "FROM outbox WHERE state='pending' ORDER BY enqueued_at DESC LIMIT 5"
):
    print(f"    id={r['id']} type={r['type']} enqueued={fmt_age(r['enqueued_at'])} attempts={r['attempts']} last_attempt={r['last_attempt_at'] or 'never'}")

# Items pending más viejos (los que llevan mucho sin despacharse)
print("\n  Items pending (más viejos, top 5):")
for r in conn.execute(
    "SELECT id, type, enqueued_at, attempts, last_attempt_at "
    "FROM outbox WHERE state='pending' ORDER BY enqueued_at ASC LIMIT 5"
):
    print(f"    id={r['id']} type={r['type']} enqueued={fmt_age(r['enqueued_at'])} attempts={r['attempts']}")

# ========================================================================
# 3. Eventos críticos / errores recientes
# ========================================================================
print("\n[3] WARNINGS / ERRORS últimas 24h")
cutoff_24h = (NOW - timedelta(hours=24)).isoformat()
events = conn.execute(
    "SELECT kind, severity, COUNT(*) n FROM runtime_events "
    "WHERE created_at >= ? AND severity IN ('warning','error','critical') "
    "GROUP BY kind, severity ORDER BY n DESC LIMIT 20",
    (cutoff_24h,)
).fetchall()
if events:
    for r in events:
        print(f"  {r['kind']:50s} | {r['severity']:8s} | {r['n']}")
else:
    print("  Sin warnings/errors en 24h")

# ========================================================================
# 4. Eventos del dispatcher (tick)
# ========================================================================
print("\n[4] DISPATCHER: ÚLTIMAS LLAMADAS dispatch_outbox (últimas 24h)")
disps = conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' AND created_at >= ? "
    "AND payload_json LIKE '%dispatch_outbox%' AND payload_json LIKE '%phase\":\"after%' "
    "ORDER BY id DESC LIMIT 5",
    (cutoff_24h,)
).fetchall()
if not disps:
    print("  Sin dispatch_outbox en 24h")
for r in disps:
    p = json.loads(r['payload_json'])
    rs = p.get('result_summary', {})
    print(f"  {r['created_at']} ({fmt_age(r['created_at'])})")
    print(f"    skipped={rs.get('skipped')} reason={rs.get('reason')} ticks={rs.get('ticks')} published={rs.get('published')}")

# ========================================================================
# 5. Eventos de hunt
# ========================================================================
print("\n[5] HUNTS últimas 24h (cuántos por marketplace)")
for mkt in ('amazon', 'mercadolibre'):
    n = conn.execute(
        "SELECT COUNT(*) FROM runtime_events "
        "WHERE kind='mcp_tool_called' AND created_at >= ? "
        "AND payload_json LIKE ? AND payload_json LIKE '%phase\":\"after%'",
        (cutoff_24h, f"%hunt_{mkt}%"),
    ).fetchone()[0]
    print(f"  hunt_{mkt}: {n} llamadas")

print("\n  Última hunt_amazon:")
r = conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' AND payload_json LIKE '%hunt_amazon%' "
    "AND payload_json LIKE '%phase\":\"after%' ORDER BY id DESC LIMIT 1"
).fetchone()
if r:
    print(f"    {r['created_at']} ({fmt_age(r['created_at'])})")
    p = json.loads(r['payload_json']).get('result_summary', {})
    print(f"    enqueued={p.get('enqueued')} discarded={p.get('discarded')} processed={p.get('processed')}")
else:
    print("    Nunca")

print("\n  Última hunt_mercadolibre:")
r = conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' AND payload_json LIKE '%hunt_mercadolibre%' "
    "AND payload_json LIKE '%phase\":\"after%' ORDER BY id DESC LIMIT 1"
).fetchone()
if r:
    print(f"    {r['created_at']} ({fmt_age(r['created_at'])})")
    p = json.loads(r['payload_json']).get('result_summary', {})
    print(f"    enqueued={p.get('enqueued')} discarded={p.get('discarded')} processed={p.get('processed')}")
else:
    print("    Nunca")

# ========================================================================
# 6. Curator decisions
# ========================================================================
print("\n[6] DiversityCurator decisions (todas las que existan)")
total = conn.execute(
    "SELECT COUNT(*) FROM runtime_events WHERE kind='diversity_curator_decision'"
).fetchone()[0]
print(f"  Total: {total}")
if total > 0:
    print("  Últimas 5:")
    for r in conn.execute(
        "SELECT created_at, payload_json FROM runtime_events "
        "WHERE kind='diversity_curator_decision' ORDER BY id DESC LIMIT 5"
    ):
        p = json.loads(r['payload_json'])
        print(f"    {r['created_at']} chosen={p.get('chosen_id')} fallback={p.get('fallback_used')} reason={p.get('reason')[:50]} latency={p.get('llm_latency_ms')}ms")

# ========================================================================
# 7. Última actividad MCP
# ========================================================================
print("\n[7] ÚLTIMA actividad MCP (cualquier tool)")
r = conn.execute(
    "SELECT created_at, payload_json FROM runtime_events "
    "WHERE kind='mcp_tool_called' ORDER BY id DESC LIMIT 1"
).fetchone()
if r:
    p = json.loads(r['payload_json'])
    print(f"  {r['created_at']} ({fmt_age(r['created_at'])})")
    print(f"  Tool: {p.get('tool')} phase={p.get('phase')}")
else:
    print("  Sin actividad MCP")

# ========================================================================
# 8. Resumen temporal: actividad por hora últimas 24h
# ========================================================================
print("\n[8] PUBLICACIONES por hora (últimas 24h)")
rows = conn.execute(
    "SELECT substr(sent_at,1,13) hour, COUNT(*) n, SUM(success) ok "
    "FROM published_messages WHERE sent_at >= ? "
    "GROUP BY hour ORDER BY hour DESC",
    (cutoff_24h,)
).fetchall()
if rows:
    for r in rows:
        print(f"  {r['hour']:14s} | total={r['n']:3d} | ok={r['ok'] or 0}")
else:
    print("  Sin publicaciones en 24h")

# ========================================================================
# 9. Errors de publish específicos
# ========================================================================
print("\n[9] PUBLISH FAILURES (success=0) últimas 24h")
fails = conn.execute(
    "SELECT id, sent_at, evolution_response, message_text FROM published_messages "
    "WHERE success = 0 AND sent_at >= ? ORDER BY id DESC LIMIT 10",
    (cutoff_24h,)
).fetchall()
if fails:
    for r in fails:
        ev = (r['evolution_response'] or '')[:200]
        print(f"  {r['sent_at']} ({fmt_age(r['sent_at'])})")
        print(f"    evolution: {ev}")
        print(f"    text: {(r['message_text'] or '')[:80]}")
else:
    print("  Sin failures en 24h")

# ========================================================================
# 10. Agent runs (heartbeats)
# ========================================================================
print("\n[10] AGENT RUNS (heartbeats) últimos 5")
for r in conn.execute(
    "SELECT id, agent_name, status, started_at, last_heartbeat, ended_at "
    "FROM agent_runs ORDER BY id DESC LIMIT 5"
):
    cols = dict(r)
    print(f"  id={cols['id']} agent={cols['agent_name']} status={cols['status']} started={fmt_age(cols['started_at'])} last_hb={fmt_age(cols['last_heartbeat'])} ended={cols['ended_at'] or 'still alive'}")

# ========================================================================
# 11. Frontier status
# ========================================================================
print("\n[11] FRONTIER STATUS")
for r in conn.execute(
    "SELECT marketplace, url_type, COUNT(*) n FROM frontier "
    "GROUP BY marketplace, url_type ORDER BY marketplace, n DESC"
):
    print(f"  {r['marketplace']:14s} | {r['url_type']:12s} | {r['n']}")

# ========================================================================
# 12. Schedule mode check
# ========================================================================
print("\n[12] HORA LOCAL CDMX vs Schedule")
import zoneinfo
local = NOW.astimezone(zoneinfo.ZoneInfo("America/Mexico_City"))
print(f"  Hora CDMX: {local.strftime('%H:%M:%S')} ({local.strftime('%A')})")
print(f"  Hibernación: 23:30-06:30")
print(f"  Warmup: 06:30-07:00")
print(f"  Active: 07:00-23:30")
hour_min = local.hour * 60 + local.minute
if 23*60+30 <= hour_min or hour_min < 6*60+30:
    print(f"  >>> Estamos en HIBERNATING (NO publica)")
elif 6*60+30 <= hour_min < 7*60:
    print(f"  >>> Estamos en WARMUP (NO publica)")
else:
    print(f"  >>> Estamos en ACTIVE (sí debería publicar)")
