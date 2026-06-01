"""Debug: simula exactamente lo que hace el dispatcher al buscar items elegibles."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
sys.path.insert(0, str(Path("/opt/deal-agent/ofertas_hunter/src")))

from ofertas_hunter.dispatching.outbox import OutboxConfig, SqliteOutbox
from ofertas_hunter.dispatching.cooldown import CooldownPolicy

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

# Restaurar last_normal_publication_at como lo hace el dispatcher
row = conn.execute(
    "SELECT sent_at FROM published_messages WHERE success=1 ORDER BY id DESC LIMIT 1"
).fetchone()
last_pub = None
if row:
    last_pub = datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
    delta = (datetime.now(timezone.utc) - last_pub).total_seconds()
    print(f"last_normal_publication_at: {row['sent_at']} (hace {int(delta)}s)")
else:
    print("last_normal_publication_at: None")

config = OutboxConfig(
    revalidate_age_seconds=3600,
    cooldown=CooldownPolicy(cooldown_seconds=300),
)
outbox = SqliteOutbox(conn, config)

now = datetime.now(timezone.utc)
pending = outbox.pending()
print(f"\nPending items: {len(pending)}")

eligible = outbox.eligible_now(last_pub, now)
print(f"Eligible items: {len(eligible)}")

if not eligible:
    print("\nDEBUG: por qué no hay elegibles:")
    for item in pending[:10]:
        publishable = config.cooldown.is_publishable(item.type, last_pub, now)
        scheduled_ok = not (item.scheduled_for and item.scheduled_for > now)
        print(f"  ob#{item.id} type={item.type} publishable={publishable} scheduled_ok={scheduled_ok}")
        if not publishable:
            from ofertas_hunter.dispatching.cooldown import CooldownPolicy as CP
            cp = CP(cooldown_seconds=300)
            print(f"    cooldown check: type={item.type} last_pub={last_pub}")
else:
    print(f"\nTop 5 elegibles:")
    for item in eligible[:5]:
        p = item.message_payload or {}
        print(f"  ob#{item.id} type={item.type} disc={p.get('discount_percent')}% prev={p.get('previous_price')} '{(p.get('title') or '')[:50]}'")
