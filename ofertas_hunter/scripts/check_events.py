import sqlite3, sys
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-31T12:40:00Z"
c = sqlite3.connect("file:data/ofertas_hunter.db?mode=ro", uri=True, timeout=20)
c.row_factory = sqlite3.Row
c.execute("PRAGMA busy_timeout=20000")
print(f"=== eventos desde {SINCE} ===")
for k in ["curated_seed_reseeded", "frontier_priority_rebalanced", "pool_deficit_plan",
          "frontier_category_bias_applied", "ml_discovery_prefilter"]:
    n = c.execute("SELECT COUNT(*) FROM runtime_events WHERE kind=? AND created_at>=?", (k, SINCE)).fetchone()[0]
    print(f"  {k}: {n}")
print("\n=== ultimo curated_seed_reseeded ===")
r = c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='curated_seed_reseeded' ORDER BY id DESC LIMIT 1").fetchone()
print(" ", r["created_at"], r["payload_json"][:160]) if r else print("  (ninguno)")
print("\n=== ultimo frontier_priority_rebalanced ===")
r = c.execute("SELECT created_at, payload_json FROM runtime_events WHERE kind='frontier_priority_rebalanced' ORDER BY id DESC LIMIT 1").fetchone()
print(" ", r["created_at"], r["payload_json"][:160]) if r else print("  (ninguno)")
print("\n=== frontier: deal pages score ===")
for r in c.execute("SELECT url_type, score, COUNT(*) n FROM frontier WHERE score>=15 GROUP BY url_type, score ORDER BY score DESC LIMIT 8"):
    print(f"  type={r['url_type']} score={r['score']} n={r['n']}")
