#!/usr/bin/env python3
"""Read-only: verificar pipeline de recovery ML (cookies por Telegram) y si ML revivio."""
import sqlite3, json, datetime
c = sqlite3.connect("file:data/ofertas_hunter.db?mode=ro", uri=True, timeout=30)
c.execute("PRAGMA busy_timeout=30000")
now = datetime.datetime.now(datetime.timezone.utc)
def ago(m): return (now-datetime.timedelta(minutes=m)).isoformat(timespec="milliseconds").replace("+00:00","Z")
print("now:", now.isoformat(timespec='seconds'))

print("\n=== pipeline recovery: eventos ML ult 60 min (orden cronologico) ===")
rows=c.execute(
    "SELECT created_at,kind,substr(payload_json,1,110) FROM runtime_events "
    "WHERE created_at>=? AND kind IN "
    "('ml_cookies_received','ml_cookies_staged','ml_cookies_validated','ml_cookies_promoted',"
    "'ml_session_context_rotated','ml_session_state_changed','ml_cookies_reloaded',"
    "'ml_session_admin_alerted','ml_session_active_validation','ml_session_health') "
    "ORDER BY id ASC",(ago(60),)
).fetchall()
if not rows: print("  (sin eventos de recovery en 60min)")
for r in rows:
    print(f"  [{r[0][:19]}] {r[1]}: {r[2]}")

print("\n=== ml_session_health: ultimos 4 ===")
rows=c.execute("SELECT created_at,payload_json FROM runtime_events WHERE kind='ml_session_health' ORDER BY id DESC LIMIT 4").fetchall()
for r in rows:
    p=json.loads(r[1]); print(f"  [{r[0][:19]}] status={p.get('status')} cookies={p.get('cookies_count')} redirected={str(p.get('redirected_to'))[:45]}")

print("\n=== cookie_expiry: ultimo + conteo reciente (sigue fallando discovery?) ===")
last=c.execute("SELECT MAX(created_at) FROM runtime_events WHERE kind='cookie_expiry'").fetchone()[0]
print("  ultimo cookie_expiry:", last)
for label,m in (("5min",5),("15min",15)):
    n=c.execute("SELECT COUNT(*) FROM runtime_events WHERE kind='cookie_expiry' AND created_at>=?",(ago(m),)).fetchone()[0]
    print(f"  cookie_expiry {label}: {n}")

print("\n=== ML productos nuevos: revivio? ===")
for label,m in (("5min",5),("15min",15),("30min",30)):
    n=c.execute("SELECT COUNT(*) FROM products WHERE marketplace='mercadolibre' AND first_seen_at>=?",(ago(m),)).fetchone()[0]
    print(f"  ML nuevos {label}: {n}")
row=c.execute("SELECT MAX(first_seen_at) FROM products WHERE marketplace='mercadolibre'").fetchone()
print("  ultimo ML product:", row[0])
if row[0]:
    try:
        last2=datetime.datetime.fromisoformat(str(row[0]).replace("Z","+00:00"))
        print(f"  hace {(now-last2).total_seconds()/60:.1f} min")
    except: pass

print("\n=== cookies file mtime (se actualizo con tu inyeccion?) ===")
import os
for f in ("secrets/mercadolibre_cookies.json","secrets/incoming_cookies/mercadolibre_latest.json"):
    try:
        m=os.path.getmtime(f)
        print(f"  {f}: {datetime.datetime.fromtimestamp(m,datetime.timezone.utc).isoformat(timespec='seconds')}")
    except Exception as e: print(f"  {f}: {e}")
c.close()
