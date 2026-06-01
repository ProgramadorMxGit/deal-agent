"""Descarta outbox items sin previous_price o discount_percent."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/opt/deal-agent/ofertas_hunter/data/ofertas_hunter.db")
if not DB.exists():
    DB = Path("data/ofertas_hunter.db")

c = sqlite3.connect(str(DB))
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

r = c.execute(
    """UPDATE outbox SET state='discarded', last_attempt_at=?
       WHERE state='pending' AND type='normal'
         AND (
           json_extract(message_payload_json,'$.previous_price') IS NULL
           OR json_extract(message_payload_json,'$.discount_percent') IS NULL
         )""",
    (now,)
)
c.commit()
print(f"Descartados (sin prev_price/discount): {r.rowcount}")

total = c.execute("SELECT COUNT(*) FROM outbox WHERE state='pending'").fetchone()[0]
print(f"Outbox pending restante: {total}")
