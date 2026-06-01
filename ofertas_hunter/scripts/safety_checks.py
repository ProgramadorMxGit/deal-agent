import os, sqlite3, time
DB = "data/ofertas_hunter.db"
pub = "src/ofertas_hunter/publishing/whatsapp_publisher.py"
st = os.stat(pub)
print("publisher mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime)), "UTC")
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
c.execute("PRAGMA busy_timeout=20000")
since = "2026-05-30T22:45:00Z"
n_null = c.execute(
    "SELECT COUNT(*) FROM published_messages WHERE success=1 AND sent_at>=? AND outbox_id IS NULL",
    (since,),
).fetchone()[0]
n_total = c.execute(
    "SELECT COUNT(*) FROM published_messages WHERE success=1 AND sent_at>=?",
    (since,),
).fetchone()[0]
print(f"published since deploy: {n_total} | with NULL outbox_id (manual?): {n_null}")
