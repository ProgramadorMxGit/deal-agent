"""Diagnóstico: detecta ofertas duplicadas publicadas recientemente."""
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB))
c.row_factory = sqlite3.Row

# Últimas 6 horas de publicaciones exitosas
cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")

print(f"=== Publicaciones exitosas últimas 6h (desde {cutoff}) ===")
rows = c.execute(
    """
    SELECT pm.id pm_id, pm.outbox_id, pm.sent_at, pm.success,
           o.type otype, o.message_payload_json payload
    FROM published_messages pm
    JOIN outbox o ON pm.outbox_id = o.id
    WHERE pm.success = 1 AND pm.sent_at >= ?
    ORDER BY pm.sent_at DESC
    """,
    (cutoff,),
).fetchall()

print(f"Total enviadas exitosas: {len(rows)}")
print()

# Agrupar por item_id / asin / url
by_id = defaultdict(list)
by_url = defaultdict(list)
by_title = defaultdict(list)

for r in rows:
    try:
        p = json.loads(r["payload"])
    except Exception:
        continue
    iid = p.get("item_id") or p.get("asin") or "(sin id)"
    url = p.get("canonical_url") or p.get("url") or ""
    title = (p.get("title") or "")[:80]
    by_id[iid].append((r["pm_id"], r["sent_at"], r["otype"], title, url))
    by_url[url].append((r["pm_id"], r["sent_at"], r["otype"], iid, title))
    by_title[title].append((r["pm_id"], r["sent_at"], r["otype"], iid, url))

print("=== Duplicados por item_id/asin (mismo producto enviado más de 1 vez) ===")
dup_id = {k: v for k, v in by_id.items() if len(v) > 1 and k != "(sin id)"}
print(f"Productos con duplicados: {len(dup_id)}")
for iid, items in sorted(dup_id.items(), key=lambda x: -len(x[1]))[:15]:
    print(f"\n  item_id={iid}  ({len(items)} envíos):")
    for pm_id, sent_at, otype, title, url in items:
        print(f"    [{otype}] pm#{pm_id} {sent_at}  '{title[:60]}'")

print()
print("=== Duplicados por URL ===")
dup_url = {k: v for k, v in by_url.items() if len(v) > 1 and k}
print(f"URLs duplicadas: {len(dup_url)}")
for url, items in sorted(dup_url.items(), key=lambda x: -len(x[1]))[:5]:
    print(f"\n  url={url[:100]}  ({len(items)} envíos):")
    for pm_id, sent_at, otype, iid, title in items:
        print(f"    [{otype}] pm#{pm_id} {sent_at}  iid={iid}")

print()
print("=== Duplicados por título exacto ===")
dup_title = {k: v for k, v in by_title.items() if len(v) > 1 and k}
print(f"Títulos duplicados: {len(dup_title)}")
for title, items in sorted(dup_title.items(), key=lambda x: -len(x[1]))[:5]:
    print(f"\n  title='{title}'  ({len(items)} envíos):")
    for pm_id, sent_at, otype, iid, url in items:
        print(f"    [{otype}] pm#{pm_id} {sent_at}  iid={iid}")

# Tasa total
print()
print("=== Resumen ===")
total = len(rows)
unique_ids = len([k for k in by_id if k != "(sin id)"])
total_dups = sum(len(v) - 1 for v in by_id.values() if len(v) > 1 and k != "(sin id)")
print(f"Total publicaciones: {total}")
print(f"item_ids únicos: {unique_ids}")
print(f"Repeticiones (envíos extras del mismo item): {total_dups}")
