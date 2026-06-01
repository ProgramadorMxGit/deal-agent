import sqlite3
DB = "data/ofertas_hunter.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=30000")

print("=== login_redirect snapshots por hora (ultimas) ===")
for r in c.execute("SELECT substr(captured_at,1,13) h, COUNT(*) n FROM dom_snapshots WHERE reason='login_redirect' GROUP BY h ORDER BY h DESC LIMIT 8"):
    print(f"  {r['h']}: {r['n']}")

print("\n=== ml_session runtime_events recientes ===")
for r in c.execute("SELECT kind, COUNT(*) n FROM runtime_events WHERE kind LIKE '%ml_session%' OR kind LIKE '%cookie%' GROUP BY kind ORDER BY n DESC LIMIT 10"):
    print(f"  {r['n']:>5}  {r['kind']}")

print("\n=== ultimos cookie_expiry / ml_session events ===")
for r in c.execute("SELECT created_at, kind, payload_json FROM runtime_events WHERE kind LIKE '%cookie%' OR kind LIKE '%ml_session%' ORDER BY id DESC LIMIT 5"):
    print(f"  {r['created_at']} {r['kind']} {(r['payload_json'] or '')[:80]}")

print("\n=== discarded_candidates login_redirect recientes ===")
r = c.execute("SELECT COUNT(*) n FROM discarded_candidates WHERE reason='login_redirect' AND created_at >= '2026-05-31T00:00:00Z'").fetchone()
print(f"  login_redirect descartes hoy: {r['n']}")
